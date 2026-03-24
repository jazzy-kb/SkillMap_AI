# test_gen_final.py
from google import genai
import os
from dotenv import load_dotenv
load_dotenv()

client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

response = client.models.generate_content(
    model="gemini-1.5-flash",
    contents="Write a motivational 2-line quote about learning new skills."
)

print(response.text)
