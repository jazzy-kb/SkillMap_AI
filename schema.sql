PRAGMA foreign_keys = ON;

-- Users table
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    email TEXT UNIQUE,
    joined_at TEXT DEFAULT (datetime('now')),
    points INTEGER DEFAULT 0,
    level TEXT DEFAULT 'Novice'
);

-- Skills master list (canonical skill names)
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    description TEXT
);

-- Courses table (mapping skills -> learning resources)
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id INTEGER REFERENCES skills(id) ON DELETE SET NULL,
    title TEXT,
    provider TEXT,
    url TEXT,
    estimated_hours INTEGER,
    level TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- User resume / profile storage (optional small text)
CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    filename TEXT,
    text_content TEXT,
    uploaded_at TEXT DEFAULT (datetime('now'))
);

-- user_skill: user's claimed/extracted skills and verification status
CREATE TABLE IF NOT EXISTS user_skill (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    skill_id INTEGER REFERENCES skills(id) ON DELETE CASCADE,
    source TEXT,            -- e.g., "resume", "user_input", "quiz"
    verified INTEGER DEFAULT 0,
    verified_at TEXT,
    last_updated TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, skill_id)
);

-- user_course: user's enrollments and progress on recommended courses
CREATE TABLE IF NOT EXISTS user_course (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'not_started', -- not_started, in_progress, completed
    progress INTEGER DEFAULT 0,        -- 0-100 percent
    enrolled_at TEXT,
    completed_at TEXT
);

-- Quiz bank (predefined + AI-generated questions)
CREATE TABLE IF NOT EXISTS quiz_bank (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id INTEGER REFERENCES skills(id) ON DELETE SET NULL,
    question TEXT,
    choices TEXT,       -- JSON string of options
    answer TEXT,
    source TEXT,        -- 'human' or 'ai'
    created_at TEXT DEFAULT (datetime('now'))
);

-- Quiz attempts by users
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    quiz_id INTEGER REFERENCES quiz_bank(id) ON DELETE SET NULL,
    score INTEGER,
    taken_at TEXT DEFAULT (datetime('now'))
);

-- Badges / achievements
CREATE TABLE IF NOT EXISTS badges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    description TEXT,
    points_reward INTEGER DEFAULT 0
);

-- user_badges
CREATE TABLE IF NOT EXISTS user_badges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    badge_id INTEGER REFERENCES badges(id) ON DELETE CASCADE,
    awarded_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, badge_id)
);

-- Indexes for performance (small dataset but useful)
CREATE INDEX IF NOT EXISTS idx_user_course_user ON user_course(user_id);
CREATE INDEX IF NOT EXISTS idx_user_skill_user ON user_skill(user_id);
CREATE INDEX IF NOT EXISTS idx_quiz_skill ON quiz_bank(skill_id);
