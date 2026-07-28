import os, bcrypt
from dotenv import load_dotenv
from db_core import db_manager
from sqlalchemy import text
load_dotenv()
db_manager.initialize(os.getenv('DB_DIALECT', 'POSTGRESQL'), os.getenv('DB_URL'))
username = input('Enter username to reset: ').strip().lower()
hashed = bcrypt.hashpw(username.encode(), bcrypt.gensalt()).decode()
with db_manager.get_connection() as conn:
    conn.execute(text('UPDATE employee SET password_hash = :h WHERE LOWER(systemloginname) = :u'), {'h': hashed, 'u': username})
    conn.commit()
print(f'Successfully reset password for {username} to default ({username})')
#conda run -n isoworks python backend/scripts/seed_passwords.py

