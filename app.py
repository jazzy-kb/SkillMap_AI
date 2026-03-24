import streamlit as st
import pandas as pd
import io
import pdfplumber
import docx
import sqlite3
import json
import os
import re
from datetime import datetime, timezone 
from dotenv import load_dotenv
from PIL import Image
import pytesseract
from PyPDF2 import PdfReader
from fpdf import FPDF
import base64
import unicodedata

try:
    import google.generativeai as genai
    from google import genai as genai_client
    AI_AVAILABLE = True
except Exception:
    AI_AVAILABLE = False

load_dotenv()  
DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "skillmap.db")
SKILLS_CSV = os.path.join(DATA_DIR, "skills.csv")
COURSES_CSV = os.path.join(DATA_DIR, "courses.csv")
QUIZ_JSON = os.path.join(DATA_DIR, "quiz_bank.json")
AI_QUIZ_CACHE = os.path.join(DATA_DIR, "ai_quizzes.json")
AI_PLAN_DIR = os.path.join(DATA_DIR, "ai_plans")
os.makedirs(AI_PLAN_DIR, exist_ok=True)

if AI_AVAILABLE:
    API_KEY = os.getenv("GOOGLE_API_KEY")
    if API_KEY:
        genai.configure(api_key=API_KEY)
        client = genai_client.Client(api_key=API_KEY) 
    else:
        AI_AVAILABLE = False

MODEL_NAME = "gemini-2.5-flash"

USER_ID = 1

def ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
ensure_data_dir()

def normalize_text(t: str) -> str:
    return re.sub(r'[^a-z0-9 ]', ' ', (t or "").lower())

def load_skills():
    if not os.path.exists(SKILLS_CSV): return []
    return [line.strip().lower() for line in open(SKILLS_CSV, encoding='utf-8') if line.strip()]

def loadCourses():
    if not os.path.exists(COURSES_CSV):
        return pd.DataFrame(columns=["skill","provider","title","url"])
    return pd.read_csv(COURSES_CSV)

def loadQuizbank():
    if not os.path.exists(QUIZ_JSON): return {}
    return json.load(open(QUIZ_JSON, encoding='utf-8'))

def extractSkills(text, skills):
    text = normalize_text(text)
    found = []
    for s in skills:
        if s in text:
            found.append(s)
    return sorted(set(found))

def parseResume(uploadedfile):
    if uploadedfile is None: return ""
    fname = uploadedfile.name.lower()
    data = uploadedfile.read()
    if fname.endswith(".txt"):
        try: return data.decode("utf-8", errors="ignore")
        except: return data.decode("latin-1", errors="ignore")
    if fname.endswith(".docx"):
        try:
            doc = docx.Document(io.BytesIO(data))
            return "\n".join([p.text for p in doc.paragraphs])
        except Exception as e:
            print("DOCX parse error:", e)
            return ""
    if fname.endswith(".pdf"):
        try:
            textPages = []
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages:
                    txt = page.extract_text()
                    if txt: textPages.append(txt)
            all_text = "\n".join(textPages).strip()
            if all_text: return all_text
        except Exception: pass
        try: 
            reader = PdfReader(io.BytesIO(data))
            pages_text = [p.extract_text() or "" for p in reader.pages]
            all_text = "\n".join(pages_text).strip()
            if all_text: return all_text
        except Exception: pass
        try: 
            ocr_texts = []
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages:
                    img = page.to_image(resolution=150).original
                    ocr_texts.append(pytesseract.image_to_string(img))
            return "\n".join(ocr_texts)
        except Exception as e:
            print("OCR failed or pytesseract not available:", e)
            return ""
    try: return data.decode("utf-8", errors="ignore")
    except: return data.decode("latin-1", errors="ignore")

def strip_pii(text):
    text = re.sub(r'\S+@\S+\.\S+', '[email]', text)
    text = re.sub(r'\+?\d[\d\s\-\(\)]{6,}\d', '[phone]', text)
    return text

def sanitizeAts(text):
    t = text or ""
    t = re.sub(r'\S+@\S+\.\S+', '[email]', t)
    t = re.sub(r'\+?\d[\d\s\-\(\)]{6,}\d', '[phone]', t)
    return t[:4000]


def genai_generate(client, prompt, max_output_tokens=512, temperature=0.0):
    """Call Gemini and return text, or None on error."""
    if not AI_AVAILABLE:
        return None
    try:
        resp = client.models.generate_content(
            contents=prompt, 
            model=MODEL_NAME, 
            config={
                "candidate_count": 1,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens
            }
        )
        
        text = getattr(resp, "text", None)
        if text: return text
        return str(resp)
    except Exception as e:
        print("Gemini error:", e)
        return None

def genaiJson(client, prompt, schema_example, max_tokens=512, temperature=0.0):
    """Ask model to return JSON matching example. Returns parsed JSON or None."""
    if not AI_AVAILABLE: return None
    
    example = json.dumps(schema_example, indent=2)
    wrapper = (f"Return ONLY valid JSON that matches the example schema exactly. "
               f"Do NOT include any explanation or extra text. Start and end with brackets.\n\n"
               f"EXAMPLE:\n{example}\n\nPROMPT:\n{prompt}")
    
    raw = None 

    try:
        raw = genai_generate(client, wrapper, max_output_tokens=max_tokens, temperature=temperature)
        
        if not raw: return None 
        
        raw_text = raw.strip()
        jsontext = raw_text

        if raw_text.startswith('```'):
            jsontext = re.sub(r"^\s*`{3}(json)?\s*|`{3}\s*$", "", raw_text, flags=re.DOTALL).strip()
        
        start = jsontext.find('[')
        end = jsontext.rfind(']')

        if start == -1 or end == -1: 
            start = jsontext.find('{')
            end = jsontext.rfind('}')

        if start != -1 and end != -1 and start < end:
            jsontext = jsontext[start:end+1]
        else:
            print(f"JSON Extraction failed. Raw response was: {raw}")
            return None 
        
        return json.loads(jsontext)
    except Exception as e:
        print(f"GenAI JSON parse error: {e}")
        return None

def generateQuiz(client, skill_name, num_questions=3): 
    cache = {}
    if os.path.exists(AI_QUIZ_CACHE): cache = json.load(open(AI_QUIZ_CACHE, encoding='utf-8'))
    if skill_name in cache: return cache[skill_name]
    
    prompt = f"""Generate {num_questions} multiple-choice questions to test a learner on the skill: "{skill_name}".
For each question return: - question: short question text (max 120 chars) - options: list of 4 answer options - correct: the index (0..3) of the correct option
Return a JSON array of questions. Crucially, return ONLY the JSON array (starting with '[') and nothing else."""
    
    schema_example = [{"question": "Example: What does SQL stand for?",
                       "options": ["Structured Query Language", "Simple Query Language", "Sequential Query Language", "Server Query Language"],
                       "correct": 0}]
    
    j = genaiJson(client, prompt, schema_example, max_tokens=600, temperature=0.2)
    
    if not j or not isinstance(j, list): return None
    
    cleaned = []
    for item in j[:num_questions]:
        if all(k in item for k in ("question","options","correct")) and isinstance(item["options"], list) and len(item["options"]) == 4:
            try:
                correct_idx = int(item["correct"])
            except:
                correct_idx = 0 

            cleaned.append({"question": item["question"], 
                            "options": item["options"],
                            "correct": correct_idx})
    
    if cleaned:
        cache[skill_name] = cleaned
        with open(AI_QUIZ_CACHE, "w", encoding="utf-8") as f: json.dump(cache, f, indent=2)
        
        qb = loadQuizbank()
        qb[skill_name] = cleaned
        with open(QUIZ_JSON, "w", encoding='utf-8') as f: json.dump(qb, f, indent=2)
        
        return cleaned
    return None

def generateLearningP(client, user_profile_text, skill_name, target_role=None, weeks=4):
    keyname = f"plan_{skill_name.replace(' ','_')}.json"
    path = os.path.join(AI_PLAN_DIR, keyname)
    if os.path.exists(path): return json.load(open(path, encoding='utf-8'))
    
    prompt = f"""You are an expert learning coach. Create a {weeks}-week practical learning plan for the skill: "{skill_name}".
User profile: {user_profile_text}
If target role is provided, tailor the plan to that role: {target_role}
For each week provide 2-4 actionable goals and 1-3 recommended resources (title + short url if possible).
Return JSON with keys: skill, summary, estimated_hours, weekly_plan (array), assessment. Return ONLY the JSON object (starting with '{{') and nothing else."""
    schema_example = {"skill": skill_name, "summary": "One-line summary", "estimated_hours": 20,
        "weekly_plan": [{"week": 1, "goals": ["..."], "resources": ["..."]}],
        "assessment": "One sentence"}
    
    j = genaiJson(client, prompt, schema_example, max_tokens=600, temperature=0.2)
    
    if j:
        with open(path, "w", encoding='utf-8') as f: json.dump(j, f, indent=2)
        return j
    return None

def atsMatch(client, resume_text, job_description_text, top_n=3):
    resume_clean = sanitizeAts(resume_text) 
    job_clean = sanitizeAts(job_description_text)
    
    prompt = f"""You are an ATS expert. Compare resume and job description.
Return JSON:{{ "score": <0-100 integer>, "explanation": "one paragraph", "suggestions": ["s1","s2","s3"] }}. Return ONLY the JSON object (starting with '{{') and nothing else.
Resume:{resume_clean}
Job Description:{job_clean}
"""
    example = {"score": 70, "explanation": "short", "suggestions": ["s1","s2","s3"]}
    
    j = genaiJson(client, prompt, example, max_tokens=400, temperature=0.0)
    return j

def ensure_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, points INTEGER DEFAULT 0, level TEXT DEFAULT 'Novice')""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_course (id INTEGER PRIMARY KEY, user_id INTEGER, skill TEXT, provider TEXT, title TEXT, url TEXT, status TEXT, progress INTEGER, enrolled_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_quiz (id INTEGER PRIMARY KEY, user_id INTEGER, skill TEXT, score INTEGER, taken_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS skill_ver (id INTEGER PRIMARY KEY, user_id INTEGER, skill TEXT, final_score INTEGER, status TEXT, verified_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS points_log (id INTEGER PRIMARY KEY, user_id INTEGER, points INTEGER, reason TEXT, timestamp TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS badges (id INTEGER PRIMARY KEY, code TEXT, title TEXT, description TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_badges (id INTEGER PRIMARY KEY, user_id INTEGER, badge_id INTEGER, awarded_at TEXT)""")
    conn.commit()
    return conn

conn = ensure_db()
c = conn.cursor()

def add_demo_user():
    c.execute("SELECT id FROM users WHERE id=1")
    if not c.fetchone():
        c.execute("INSERT INTO users(id,name,email,points,level) VALUES (1,?,?,0,'Novice')", ("Demo User","demo@example.com"))
        conn.commit()
add_demo_user()

def seed_badges():
    rows = c.execute("SELECT COUNT(*) FROM badges").fetchone()[0]
    if rows == 0:
        badges = [
            ("FIRST_VERIFIED","First Skill Verified","Awarded when you verify your first skill."),
            ("QUIZ_MASTER","Quiz Master","Pass 5 quizzes (>=60%)."),
            ("COURSE_FINISHER","Course Finisher","Complete 5 courses.")
        ]
        for code, title, desc in badges: c.execute("INSERT INTO badges(code,title,description) VALUES (?,?,?)",(code,title,desc))
        conn.commit()
seed_badges()

def store_recommended_courses(user_id, missing_skills, courses_df):
    c.execute("DELETE FROM user_course WHERE user_id=? AND status IN ('pending', 'in_progress')", (user_id,))
    conn.commit() 
    now = datetime.now(timezone.utc).isoformat()
    for skill in missing_skills:
        matches = courses_df[courses_df['skill'].str.lower() == skill]
        if matches.empty: continue
        for _,row in matches.head(2).iterrows():
            c.execute("SELECT id FROM user_course WHERE user_id=? AND skill=? AND title=?", (user_id, skill, row['title']))
            if not c.fetchone():
                c.execute("""INSERT INTO user_course(user_id,skill,provider,title,url,status,progress,enrolled_at)
                             VALUES (?,?,?,?,?,?,?,?)""",
                            (user_id, skill, row['provider'], row['title'], row['url'], "pending", 0, now))
    conn.commit()

def get_user_courses(user_id): return pd.read_sql_query("SELECT * FROM user_course WHERE user_id=?", conn, params=(user_id,))

def update_course_status(course_id, status, progress):
    now = datetime.now(timezone.utc).isoformat()
    c.execute("UPDATE user_course SET status=?, progress=?, enrolled_at=? WHERE id=?", (status, progress, now, course_id))
    conn.commit()

def store_quiz_result(user_id, skill, score):
    now = datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO user_quiz(user_id,skill,score,taken_at) VALUES (?,?,?,?)", (user_id, skill, score, now))
    conn.commit()

def latest_quiz_score(user_id, skill):
    row = c.execute("SELECT score FROM user_quiz WHERE user_id=? AND skill=? ORDER BY taken_at DESC LIMIT 1", (user_id, skill)).fetchone()
    return int(row[0]) if row else 0

def set_skill_verification(user_id, skill, final_score, status):
    now = datetime.now(timezone.utc).isoformat() if status == "VERIFIED" else None
    row = c.execute("SELECT id FROM skill_ver WHERE user_id=? AND skill=?", (user_id, skill)).fetchone()
    if row:
        c.execute("UPDATE skill_ver SET final_score=?, status=?, verified_at=? WHERE id=?", (final_score, status, now, row[0]))
    else:
        c.execute("INSERT INTO skill_ver (user_id,skill,final_score,status,verified_at) VALUES (?,?,?,?,?)", (user_id, skill, final_score, status, now))
    conn.commit()
    if status == "VERIFIED":
        awardPoints(user_id, 100, f"Skill Verified: {skill}")
        verified_count = c.execute("SELECT COUNT(*) FROM skill_ver WHERE user_id=? AND status='VERIFIED'", (user_id,)).fetchone()[0]
        if verified_count == 1: awardBadge(user_id, "FIRST_VERIFIED")

def latest_skill_ver(user_id, skill):
    row = c.execute("SELECT final_score,status,verified_at FROM skill_ver WHERE user_id=? AND skill=? ORDER BY id DESC LIMIT 1", (user_id, skill)).fetchone()
    return {"final_score": row[0], "status": row[1], "verified_at": row[2]} if row else {"final_score": 0, "status": "NOT_VERIFIED", "verified_at": None}

def awardPoints(user_id, points, reason):
    now = datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO points_log(user_id,points,reason,timestamp) VALUES (?,?,?,?)", (user_id, points, reason, now))
    c.execute("UPDATE users SET points = points + ? WHERE id=?", (points, user_id))
    user_points = c.execute("SELECT points FROM users WHERE id=?", (user_id,)).fetchone()[0]
    level = 'Novice'
    if user_points >= 500: level = 'Pro'
    elif user_points >= 200: level = 'Intermediate'
    c.execute("UPDATE users SET level=? WHERE id=?", (level, user_id))
    conn.commit()

def awardBadge(user_id, badge_code):
    row = c.execute("SELECT id FROM badges WHERE code=?", (badge_code,)).fetchone()
    if not row: return
    badge_id = row[0]
    already = c.execute("SELECT id FROM user_badges WHERE user_id=? AND badge_id=?", (user_id, badge_id)).fetchone()
    if already: return
    now = datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO user_badges(user_id,badge_id,awarded_at) VALUES (?,?,?)", (user_id, badge_id, now))
    conn.commit()

def get_user_profile(user_id):
    row = c.execute("SELECT id,name,email,points,level FROM users WHERE id=?", (user_id,)).fetchone()
    return {"id": row[0], "name": row[1], "email": row[2], "points": row[3], "level": row[4]} if row else None

def get_user_badges(user_id):
    rows = c.execute("""SELECT b.code,b.title,b.description,ub.awarded_at
                         FROM user_badges ub JOIN badges b ON ub.badge_id = b.id
                         WHERE ub.user_id=?""", (user_id,)).fetchall()
    return [{"code": r[0], "title": r[1], "description": r[2], "awarded_at": r[3]} for r in rows]

def computescore(P, Q, R): return int(round(0.5*P + 0.4*Q + 0.1*R))
def determine_status(final_score):
    if final_score >= 75: return "VERIFIED"
    if final_score >= 50: return "IN_PROGRESS"
    return "NOT_VERIFIED"

def generatePdf(text):
    cleaned_text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.multi_cell(0, 5, cleaned_text) 
    return pdf.output(dest='S').encode('latin-1')

def downloadPdfbutton(text):
    pdf_data = generatePdf(text)
    b64 = base64.b64encode(pdf_data).decode()
    href = f'<a href="data:application/pdf;base64,{b64}" download="cover_letter.pdf">📥 Download as PDF</a>'
    st.markdown(href, unsafe_allow_html=True)


st.set_page_config(page_title="SkillMap AI Master App", layout="wide")

skills = load_skills()
courses_df = loadCourses()
quiz_bank = loadQuizbank()
if 'activequiz' not in st.session_state: st.session_state['activequiz'] = None
if 'courseIDcomplete' not in st.session_state: st.session_state['courseIDcomplete'] = None

with st.sidebar:
    selected_page = st.selectbox("Select Application", ["SkillMap Dashboard", "Cover Letter Generator ✉️"])
    st.markdown("---")
    
    st.header("Profile")
    profile = get_user_profile(USER_ID)
    if profile:
        st.write(f"**{profile['name']}**")
        st.write(profile['email'])
        st.write("Points:", profile['points'], " | Level:", profile['level'])
    else: st.write("No profile")
    
    st.subheader("Badges")
    badges = get_user_badges(USER_ID)
    if badges:
        for b in badges: st.write(f"🏅 {b['title']} — {b['awarded_at']}")
    else: st.write("No badges yet.")
    
    st.markdown("---")
    st.subheader("Demo Controls")
    if st.button("Reset demo DB"):
        try: conn.close()
        except: pass
        # --- FIX for PermissionError ---
        try:
            if 'conn' in locals() and conn:
                conn.close()
        except Exception as e:
            print("Warning: could not close DB connection:", e)
        if os.path.exists(DB_PATH):
            try:
                os.remove(DB_PATH)
                print("Database file removed successfully.")
            except PermissionError:
                print("⚠️ Could not delete database file — it might still be in use. Please stop other running Streamlit/Python instances.")

        if os.path.exists(DB_PATH): os.remove(DB_PATH)
        if os.path.exists(AI_QUIZ_CACHE): os.remove(AI_QUIZ_CACHE)
        if os.path.exists(QUIZ_JSON): os.remove(QUIZ_JSON)
        for f in os.listdir(AI_PLAN_DIR):
            os.remove(os.path.join(AI_PLAN_DIR, f))

        conn = ensure_db()
        c = conn.cursor()
        add_demo_user()
        seed_badges()
        st.rerun() 
    
    st.markdown("---")
    st.subheader("AI Status")
    st.write("Gemini available:", AI_AVAILABLE)
    if AI_AVAILABLE: st.write("Model:", MODEL_NAME)


def render_skillmap_dashboard(skills, courses_df, quiz_bank):
    st.title("SkillMap AI — Dashboard (with Gamification & Gemini)")

    st.header("1) Upload / Paste Resume (or use sample)")
    uploaded = st.file_uploader("Upload resume file (.txt, .pdf, .docx) or paste below", type=["txt","pdf","docx"])
    resume_text_from_file = ""
    if uploaded:
        with st.spinner("Parsing uploaded file..."):
            resume_text_from_file = parseResume(uploaded)
            if not resume_text_from_file:
                st.warning("Could not extract text from the uploaded file.")
    resume_area = st.text_area("Or paste resume text here", value=resume_text_from_file or "", height=150)

    st.header("2) Paste Target Job Description")
    job_area = st.text_area("Job description text", height=150)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Analyze (Extract skills & Recommend)"):
            user_skills = extractSkills(resume_area, skills)
            job_skills  = extractSkills(job_area, skills)
            missing = [s for s in job_skills if s not in user_skills]
            st.session_state['user_skills'] = user_skills
            st.session_state['job_skills']  = job_skills
            st.session_state['missing']     = missing
            store_recommended_courses(USER_ID, missing, courses_df)
            st.success("Analysis done. Recommendations stored.")
    with col2:
        if st.button("Upload as 'after-learning' resume (evidence)"):
            user_skills_after = extractSkills(resume_area, skills)
            st.session_state['user_skills_after'] = user_skills_after
            st.success("After-learning resume stored (used as R evidence).")
        
        if AI_AVAILABLE and st.button("AI: Score Resume vs Job (ATS)"):
            with st.spinner("Running ATS match (Gemini)..."):
                ats = atsMatch(client, resume_area, job_area) 
                if ats:
                    st.subheader("ATS Score")
                    st.write("Score:", ats.get("score"))
                    st.write("Explanation:", ats.get("explanation"))
                    st.write("Suggestions:")
                    for s in ats.get("suggestions", []): st.write("•", s)
                else:
                    st.error("ATS analysis failed or AI not available. Check API key and logs.")

    st.markdown("---")
    st.header("Dashboard")

    u_sk = st.session_state.get('user_skills', [])
    j_sk = st.session_state.get('job_skills', [])
    missing = st.session_state.get('missing', [])

    if 'user_skills' not in st.session_state:
        st.session_state['user_skills'] = []; st.session_state['job_skills'] = []; st.session_state['missing'] = []
        u_sk = []; j_sk = []; missing = []

    st.subheader("Extracted Skills")
    st.write("User skills:", u_sk)
    st.write("Job skills:", j_sk)
    st.write("Missing skills:", missing)

    st.subheader("Recommended Courses")
    uc_df = get_user_courses(USER_ID)

    if missing: uc_df = uc_df[uc_df['skill'].isin(missing)]

    if uc_df.empty:
        st.info("No recommendations yet. Click Analyze to find your skill gaps.")
    else:
        for _,row in uc_df.iterrows():
            current_status = row['status']
            with st.container(border=True):
                st.markdown(f"**Skill:** {row['skill'].title()}  |  **Course:** {row['title']} ({row['provider']}) | **Status:** {current_status.upper()}")
                
                cols = st.columns([1.5, 2, 2, 1, 3]) 

                if current_status == 'pending':
                    if cols[0].button("Start Course", key=f"start_{row['id']}"):
                        update_course_status(row['id'], 'in_progress', 0) 
                        awardPoints(USER_ID, 10, f"Started course {row['title']}")
                        st.rerun()
                else: cols[0].write(f"Progress: {row['progress']}%")

                cols[1].link_button("Go to Course ➡️", row['url'], help="Opens the course link in a new tab.")

                if current_status != 'completed':
                    if cols[2].button("Complete (Take Quiz)", key=f"quiz_trigger_{row['id']}"):
                        st.session_state['activequiz'] = row['skill']
                        st.session_state['courseIDcomplete'] = row['id']
                        st.rerun()
                    
                    if cols[3].button("Sync", key=f"sync_{row['id']}"):
                        awardPoints(USER_ID, 10, f"Simulated course progress sync for {row['title']}") 
                        newp = min(100, int(row['progress'] or 0) + 25)
                        status = "in_progress" if newp < 100 else "completed"
                        update_course_status(row['id'], status, newp)
                        st.rerun()
                else:
                    cols[2].write("COMPLETED")
                    cols[3].write("") 

                if AI_AVAILABLE and cols[4].button("AI Plan", key=f"aiplan_{row['id']}"):
                    with st.spinner("Generating AI learning plan..."):
                        profile_text = resume_area or "Learner with basic skills"
                        plan = generateLearningP(client, profile_text, row['skill'], target_role=None, weeks=4)
                        if plan:
                            plan_path = os.path.join(AI_PLAN_DIR, f"plan_{USER_ID}_{row['skill'].replace(' ','_')}.json")
                            with open(plan_path, "w", encoding="utf-8") as f: json.dump(plan, f, indent=2)
                            st.success("AI plan generated and saved.")
                            st.json(plan)
                        else:
                            st.error("AI plan generation failed.")
                
                st.progress(int(row['progress'] or 0))


    st.markdown("---")
    st.header("Quizzes & Verification")
    
    
    for skill in missing:
        st.subheader(skill.title())
        last_score = latest_quiz_score(USER_ID, skill)
        st.write("Last quiz score:", last_score)
        
        if skill in quiz_bank:
            if st.button(f"Start Quiz: {skill}", key=f"quiz_{skill}"):
                st.session_state['activequiz'] = skill
        else:
            if AI_AVAILABLE:
                if st.button(f"AI Generate Quiz for: {skill}", key=f"genquiz_{skill}"):
                    with st.spinner("Generating quiz via Gemini..."):
                        q = generateQuiz(client, skill, num_questions=3) 
                        if q:
                            st.session_state['quizBank'] = True
                            st.success("AI quiz generated and stored. Rerunning to load questions.")
                            st.rerun()
                        else:
                            st.error("AI quiz generation failed. Check API key and logs.")
            else:
                st.info("No quiz available for this skill. AI is disabled.")

    if st.session_state.get('activequiz'):
        qskill = st.session_state['activequiz']
        st.subheader(f"Quiz: {qskill}")
        questions = quiz_bank.get(qskill, [])
        if not questions:
             st.warning("No questions available. Please use 'AI Generate Quiz' first.")
        else:
            with st.form(key=f"form_{qskill}"):
                for i,q in enumerate(questions):
                    st.write(f"Q{i+1}: {q['question']}")
                    st.radio("Choose:", q['options'], key=f"{qskill}_q_{i}")
                submitted = st.form_submit_button("Submit Quiz")
                
                if submitted:
                    correct = 0
                    for i,q in enumerate(questions):
                        chosen = st.session_state.get(f"{qskill}_q_{i}")
                        try: chosen_idx = q['options'].index(chosen)
                        except ValueError: chosen_idx = -1 
                        if chosen_idx == q['correct']: correct += 1
                        
                    score_pct = int(round(100 * correct / len(questions))) if questions else 0
                    store_quiz_result(USER_ID, qskill, score_pct)
                    
                    if score_pct >= 60:
                        awardPoints(USER_ID, 30, f"Quiz passed {qskill} ({score_pct}%)")
                        course_id = st.session_state.get('courseIDcomplete')
                        if course_id:
                            update_course_status(course_id, "completed", 100) 
                            awardPoints(USER_ID, 50, f"Completed course for skill {qskill}")
                            completed_count = c.execute("""SELECT COUNT(*) FROM user_course WHERE user_id=? AND status='completed'""", (USER_ID,)).fetchone()[0]
                            if completed_count >= 5: awardBadge(USER_ID, "COURSE_FINISHER")
                            del st.session_state['courseIDcomplete'] 
                        passed_quizzes = c.execute("SELECT COUNT(*) FROM user_quiz WHERE user_id=? AND score>=60", (USER_ID,)).fetchone()[0]
                        if passed_quizzes >= 5: awardBadge(USER_ID, "QUIZ_MASTER")
                        st.success(f"Quiz submitted. Score: {score_pct}%. Associated course marked completed.")
                    else:
                        st.error(f"Quiz submitted. Score: {score_pct}%. You need 60% to pass.")

                    st.session_state['activequiz'] = None
                    st.rerun()

    st.markdown("### Compute Verification (P=progress, Q=quiz score, R=resume evidence)")
    if st.button("Recompute Verification for all missing skills"):
        uc_df = get_user_courses(USER_ID)
        for skill in missing:
            rows = uc_df[uc_df['skill'].str.lower() == skill]
            P = int(rows['progress'].max()) if not rows.empty else 0
            Q = latest_quiz_score(USER_ID, skill)
            R = 100 if st.session_state.get('user_skills_after') and skill in st.session_state['user_skills_after'] else 0
            final = computescore(P, Q, R)
            status = determine_status(final)
            set_skill_verification(USER_ID, skill, final, status)
        st.success("Verification computed.")
        st.rerun()

    st.markdown("### Skill Verification Records")
    for skill in missing:
        v = latest_skill_ver(USER_ID, skill)
        st.write(skill.title(), "| Final Score:", v['final_score'], "| Status:", v['status'], "| Verified at:", v['verified_at'])

    st.markdown("---")
    st.caption("Demo steps: Analyze → (AI: ATS / AI Plan / Generate Quiz) → Start/Complete course → Take quiz → Recompute Verification.")


def render_cover_letter_generator():
    st.markdown("""
        <style>
            .title { text-align: center; font-size: 38px; font-weight: 700; color: #4B8BBE; margin-bottom: 10px;}
            .stButton>button { background-color: #4B8BBE; color: white; font-weight: bold; border-radius: 10px; padding: 10px 24px; font-size: 16px; }
            .footer { text-align: center; font-size: 13px; color: #666; margin-top: 30px; }
        </style>
    """, unsafe_allow_html=True)

    st.markdown("<h1 class='title'>Cover Letter Generator</h1>", unsafe_allow_html=True)
    st.markdown("---")

    with st.form("cover_letter_form"):
        job_title = st.text_input("Job Title", placeholder="e.g. Software Engineer")
        company = st.text_input("Company Name", placeholder="e.g. Google")
        experience = st.text_input("Years of Experience", placeholder="e.g. 3")
        key_skills = st.text_area("Key Skills", placeholder="e.g. Python, Data Analysis, APIs")
        achievements = st.text_area("Achievements (optional)", placeholder="e.g. Increased user retention by 20%")
        tone = st.selectbox("Tone of the Letter", ["Professional", "Persuasive", "Formal", "Friendly"])
        job_description = st.text_area("Paste the Job Description", placeholder="Copy the full job posting here...")
        submitted = st.form_submit_button("Generate Cover Letter")

    if submitted:
        if not AI_AVAILABLE:
            st.error("AI is not available. Please check your GOOGLE_API_KEY setting.")
            return

        with st.spinner(" Crafting your personalized cover letter..."):
            prompt = f"""
            Write a compelling cover letter for the following job application:

            Position: {job_title}
            Company: {company}
            Experience: {experience} years
            Key Skills: {key_skills}
            Achievements: {achievements if achievements else "Not specified"}
            Tone: {tone}

            Requirements:
            1. Professional business letter format
            2. Highlight experience, skills, and quantifiable achievements
            3. Use action verbs and be concise (300–400 words)
            4. Include a placeholder for the date and recipient name.

            Format the output strictly as the letter content, starting with the date line.
            """
            try:
                response = client.models.generate_content(
                    contents=prompt, 
                    model=MODEL_NAME 
                )
                cover_letter = response.text

                st.success("Cover letter generated successfully!")
                st.text_area("Generated Cover Letter", value=cover_letter, height=400)

                downloadPdfbutton(cover_letter)

                if job_description:
                    with st.spinner("Analyzing ATS match..."):
                        ats_prompt = f"""
                        Evaluate the following cover letter against this job description.
                        Provide: 1. An ATS match score out of 100 2. A short explanation for the score 3. Three suggestions to improve the cover letter.
                        --- COVER LETTER --- {cover_letter}
                        --- JOB DESCRIPTION --- {job_description}
                        """
                        ats_response = client.models.generate_content(
                            contents=ats_prompt, 
                            model=MODEL_NAME
                        )
                        st.markdown("---")
                        st.subheader("ATS Match Analysis")
                        st.markdown(ats_response.text)
            except Exception as e:
                 st.error(f"Cover Letter generation failed. API Error: {e}")

    st.markdown("<div class='footer'> </div>", unsafe_allow_html=True)

if 'quizBank' in st.session_state and st.session_state['quizBank']:
    quiz_bank = loadQuizbank()
    del st.session_state['quizBank']
if selected_page == "SkillMap Dashboard":
    render_skillmap_dashboard(skills, courses_df, quiz_bank)
elif selected_page == "Cover Letter Generator ✉️":  # <-- include emoji
    render_cover_letter_generator()
