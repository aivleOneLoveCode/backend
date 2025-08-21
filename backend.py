import asyncio
import json
import httpx
import os
import uuid
import bcrypt
import jwt
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Depends, status, Header
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from anthropic import Anthropic
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()

app = FastAPI(title="Claude MCP Multi-User Backend")

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# JWT 설정
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

security = HTTPBearer()

# 중단 요청 저장 (간단한 set 사용)
stop_requests = set()

# Pydantic 모델들
class UserRegister(BaseModel):
    email: str
    first_name: str
    last_name: str
    password: str
    marketing_consent: Optional[bool] = False
    analytics_consent: Optional[bool] = False

class UserLogin(BaseModel):
    email: str
    password: str

class UserConsent(BaseModel):
    marketing_consent: bool
    analytics_consent: bool

class ChatRequest(BaseModel):
    content: List[dict]  # content blocks array (text, image, document 등)
    session_id: Optional[str] = None

class ApiKeyWorkflowCreate(BaseModel):
    workflow_id: str
    name: str = "Untitled Workflow"

class ApiKeyWorkflowVerify(BaseModel):
    workflow_id: str

class ApiKeyWorkflowDelete(BaseModel):
    workflow_id: str

class WorkflowNameUpdate(BaseModel):
    name: str

class ProjectCreate(BaseModel):
    name: str

class ProjectUpdate(BaseModel):
    name: str

class WorkflowProjectUpdate(BaseModel):
    project_id: Optional[str] = None

class SessionTitleUpdate(BaseModel):
    title: str

# 응답 모델들
class MessageResponse(BaseModel):
    message: str

class UserResponse(BaseModel):
    user_id: str
    email: str
    first_name: str
    last_name: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse

# 게시판 관련 모델들
class BoardPostCreate(BaseModel):
    title: str
    description: str
    workflow_id: str
    workflow_name: str

class BoardPostUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    workflow_id: Optional[str] = None
    workflow_name: Optional[str] = None


# Database 작업 함수들
class DatabaseManager:
    def __init__(self):
        self.db_config = {
            'host': os.getenv('DB_HOST', 'postgres'),
            'port': os.getenv('DB_PORT', '5432'),
            'database': os.getenv('DB_NAME', 'backend'),
            'user': os.getenv('DB_USER', 'backend_user'),
            'password': os.getenv('DB_PASSWORD', 'backend_password')
        }
        self.init_database()
    
    def get_connection(self):
        """PostgreSQL 연결 생성 (재시도 로직 포함)"""
        max_retries = 30
        retry_delay = 1
        
        for attempt in range(max_retries):
            try:
                return psycopg2.connect(**self.db_config)
            except psycopg2.OperationalError as e:
                if attempt < max_retries - 1:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] PostgreSQL 연결 실패 (시도 {attempt + 1}/{max_retries}): {e}")
                    time.sleep(retry_delay)
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] PostgreSQL 연결 최종 실패: {e}")
                    raise
    
    def init_database(self):
        """데이터베이스 초기화 및 테이블 생성"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Users 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(255) PRIMARY KEY,
                email VARCHAR(255) UNIQUE NOT NULL,
                first_name VARCHAR(255) NOT NULL,
                last_name VARCHAR(255) NOT NULL,
                password VARCHAR(255) NOT NULL,
                api_key VARCHAR(255),
                marketing_consent BOOLEAN DEFAULT FALSE,
                analytics_consent BOOLEAN DEFAULT FALSE,
                consent_updated_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Sessions 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                session_id VARCHAR(255) PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                title VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Messages 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id VARCHAR(255) PRIMARY KEY,
                session_id VARCHAR(255) NOT NULL,
                role VARCHAR(50) NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            )
        ''')
        
        # Projects 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                project_id VARCHAR(255) PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                name VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Workflows 테이블 (workflow_id를 기본키로 사용)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS workflows (
                workflow_id VARCHAR(255) PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                project_id VARCHAR(255),
                name VARCHAR(255) NOT NULL DEFAULT 'Untitled Workflow',
                status VARCHAR(50) DEFAULT 'inactive' CHECK(status IN ('active', 'inactive')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id),
                FOREIGN KEY (project_id) REFERENCES projects (project_id)
            )
        ''')
        
        # Board Posts 테이블 (워크플로우 공유 게시판)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS board_posts (
                post_id VARCHAR(255) PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL,
                title VARCHAR(255) NOT NULL,
                description TEXT,
                workflow_id VARCHAR(255) NOT NULL,
                workflow_name VARCHAR(255) NOT NULL,
                tags TEXT,
                download_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        conn.commit()
        conn.close()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] PostgreSQL database initialized")
    
    # 유저 메서드
    def create_user(self, email: str, first_name: str, last_name: str, password: str, 
                    marketing_consent: bool = False, analytics_consent: bool = False):
        """새 사용자 생성"""
        user_id = str(uuid.uuid4())
        api_key = str(uuid.uuid4())  # API 키 생성
        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO users (user_id, email, first_name, last_name, password, api_key, 
                                 marketing_consent, analytics_consent, consent_updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ''', (user_id, email, first_name, last_name, hashed_password, api_key, 
                  marketing_consent, analytics_consent))
            
            conn.commit()
            conn.close()
            return user_id
        except psycopg2.IntegrityError:
            conn.close()
            raise HTTPException(status_code=400, detail="이미 존재하는 이메일입니다")
    
    def get_user_profile(self, user_id: str):
        """사용자 프로필 조회 (동의 정보 포함)"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            SELECT user_id, email, first_name, last_name, 
                   marketing_consent, analytics_consent, 
                   created_at, updated_at
            FROM users WHERE user_id = %s
        ''', (user_id,))
        
        user = cursor.fetchone()
        conn.close()
        
        if not user:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
        
        return user
    
    def update_user_consent(self, user_id: str, marketing_consent: bool, analytics_consent: bool):
        """사용자 동의 정보 업데이트"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE users 
            SET marketing_consent = %s, analytics_consent = %s, 
                consent_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
        ''', (marketing_consent, analytics_consent, user_id))
        
        conn.commit()
        conn.close()
        
        return {
            "message": "동의 정보가 업데이트되었습니다",
            "marketing_consent": marketing_consent,
            "analytics_consent": analytics_consent
        }
    
    def authenticate_user(self, email: str, password: str):
        """사용자 인증"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            SELECT user_id, password, first_name, last_name 
            FROM users WHERE email = %s
        ''', (email,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result and bcrypt.checkpw(password.encode('utf-8'), result["password"].encode('utf-8')):
            return {
                "user_id": result["user_id"],
                "email": email,
                "first_name": result["first_name"],
                "last_name": result["last_name"]
            }
        return None
    
    def get_user_by_id(self, user_id: str):
        """사용자 ID로 사용자 정보 조회"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            SELECT user_id, email, first_name, last_name, api_key 
            FROM users WHERE user_id = %s
        ''', (user_id,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                "user_id": result["user_id"],
                "email": result["email"],
                "first_name": result["first_name"],
                "last_name": result["last_name"],
                "api_key": result["api_key"]
            }
        return None
    
    # 채팅 메서드
    def create_session(self, user_id: str, title: str = "새 채팅", session_id: str = None):
        """새 세션 생성"""
        if not session_id:
            session_id = str(uuid.uuid4())
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO sessions (session_id, user_id, title, updated_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (session_id) DO UPDATE SET
                title = EXCLUDED.title,
                updated_at = CURRENT_TIMESTAMP
        ''', (session_id, user_id, title))
        
        conn.commit()
        conn.close()
        
        return session_id
    
    def update_session_title(self, session_id: str, title: str, user_id: str):
        """세션 제목 업데이트"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE sessions 
            SET title = %s, updated_at = CURRENT_TIMESTAMP 
            WHERE session_id = %s AND user_id = %s
        ''', (title, session_id, user_id))
        
        conn.commit()
        conn.close()
    
    def delete_session(self, session_id: str, user_id: str):
        """세션 삭제"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # 메시지 먼저 삭제
        cursor.execute('''
            DELETE FROM messages 
            WHERE session_id = %s
        ''', (session_id,))
        
        # 세션 삭제
        cursor.execute('''
            DELETE FROM sessions 
            WHERE session_id = %s AND user_id = %s
        ''', (session_id, user_id))
        
        conn.commit()
        conn.close()
    
    def save_message(self, session_id: str, role: str, content: str):
        """메시지를 데이터베이스에 저장"""
        message_id = str(uuid.uuid4())
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO messages (message_id, session_id, role, content)
            VALUES (%s, %s, %s, %s)
        ''', (message_id, session_id, role, content))
        
        # 세션 업데이트 시간 갱신
        cursor.execute('''
            UPDATE sessions 
            SET updated_at = CURRENT_TIMESTAMP 
            WHERE session_id = %s
        ''', (session_id,))
        
        conn.commit()
        conn.close()
        
        return message_id
    
    def get_user_sessions(self, user_id: str):
        """사용자의 모든 세션 목록 조회"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            SELECT session_id, title, created_at, updated_at
            FROM sessions 
            WHERE user_id = %s
            ORDER BY updated_at DESC
        ''', (user_id,))
        
        rows = cursor.fetchall()
        conn.close()
        
        sessions = []
        for row in rows:
            sessions.append({
                "session_id": row["session_id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            })
        
        return sessions
    
    def get_session_messages(self, session_id: str, user_id: str):
        """세션의 메시지 기록을 조회 (사용자 권한 확인)"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # 세션이 해당 사용자 것인지 확인
        cursor.execute('''
            SELECT session_id FROM sessions 
            WHERE session_id = %s AND user_id = %s
        ''', (session_id, user_id))
        
        if not cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=403, detail="세션에 접근할 권한이 없습니다")
        
        cursor.execute('''
            SELECT role, content FROM messages 
            WHERE session_id = %s 
            ORDER BY created_at ASC
        ''', (session_id,))
        
        rows = cursor.fetchall()
        conn.close()
        
        messages = []
        for row in rows:
            role = row["role"]
            content_json = row["content"]
            try:
                if content_json.startswith('[') or content_json.startswith('{'):
                    content = json.loads(content_json)
                    messages.append({"role": role, "content": content})
                else:
                    messages.append({"role": role, "content": [{"type": "text", "text": content_json}]})
            except json.JSONDecodeError:
                messages.append({"role": role, "content": [{"type": "text", "text": content_json}]})
        
        return messages
    
    def get_session_messages_for_frontend(self, session_id: str, user_id: str):
        """세션의 메시지 기록을 조회 (프론트엔드용 - 메타데이터 제거)"""
        # 원본 메시지 가져오기
        messages = self.get_session_messages(session_id, user_id)
        
        # 메타데이터 제거한 깔끔한 메시지 반환
        cleaned_messages = []
        for msg in messages:
            cleaned_msg = {
                "role": msg["role"],
                "content": []
            }
            
            # content 배열의 각 블록에서 메타데이터 제거
            for block in msg["content"]:
                if block.get("type") == "thinking":
                    cleaned_msg["content"].append({
                        "type": "thinking",
                        "thinking": block.get("thinking", "")
                    })
                elif block.get("type") == "text":
                    cleaned_msg["content"].append({
                        "type": "text", 
                        "text": block.get("text", "")
                    })
                elif block.get("type") == "tool_use":
                    cleaned_msg["content"].append({
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input")
                    })
                elif block.get("type") == "tool_result":
                    # tool_result content가 JSON 문자열인 경우 파싱
                    content = block.get("content")
                    if isinstance(content, str):
                        try:
                            content = json.loads(content)
                        except (json.JSONDecodeError, TypeError):
                            # JSON 파싱 실패 시 원본 문자열 유지
                            pass
                    
                    cleaned_msg["content"].append({
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id"),
                        "content": content
                    })
                elif block.get("type") == "image":
                    cleaned_msg["content"].append({
                        "type": "image",
                        "source": block.get("source")
                    })
                elif block.get("type") == "document":
                    cleaned_msg["content"].append({
                        "type": "document",
                        "name": block.get("name"),
                        "source": block.get("source")
                    })
                else:
                    # 기타 블록 타입은 type과 주요 필드만 유지
                    cleaned_block = {"type": block.get("type")}
                    for key in ["text", "data"]:
                        if key in block:
                            cleaned_block[key] = block[key]
                    cleaned_msg["content"].append(cleaned_block)
            
            cleaned_messages.append(cleaned_msg)
        
        return cleaned_messages
    
    # 워크플로우 메서드
    def create_workflow(self, user_id: str, workflow_id: str, name: str = "Untitled Workflow"):
        """새 워크플로우 생성"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO workflows (workflow_id, user_id, name)
            VALUES (%s, %s, %s)
        ''', (workflow_id, user_id, name))
        
        conn.commit()
        conn.close()
        
        return workflow_id
    
    def get_user_workflows(self, user_id: str):
        """사용자의 모든 워크플로우 목록 조회 (workflow_id, 제목, project_id, status 반환)"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            SELECT workflow_id, name, project_id, status
            FROM workflows 
            WHERE user_id = %s
            ORDER BY updated_at DESC
        ''', (user_id,))
        
        rows = cursor.fetchall()
        conn.close()
        
        workflows = []
        for row in rows:
            workflows.append({
                "workflow_id": row["workflow_id"],
                "title": row["name"],
                "project_id": row["project_id"],  # None일 수 있음
                "status": row["status"] or "inactive"  # 기본값 inactive
            })
        
        return workflows
    
    def get_user_workflows_simple(self, user_id: str):
        """API용: 사용자의 워크플로우 목록 조회 (workflow_id, 제목만 반환)"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            SELECT workflow_id, name
            FROM workflows 
            WHERE user_id = %s
            ORDER BY updated_at DESC
        ''', (user_id,))
        
        rows = cursor.fetchall()
        conn.close()
        
        workflows = []
        for row in rows:
            workflows.append({
                "workflow_id": row["workflow_id"],
                "title": row["name"]
            })
        
        return workflows
    
    def update_workflow_status(self, workflow_id: str, status: str, user_id: str):
        """워크플로우 상태 업데이트"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE workflows 
            SET status = %s, updated_at = CURRENT_TIMESTAMP 
            WHERE workflow_id = %s AND user_id = %s
        ''', (status, workflow_id, user_id))
        
        rows_affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        return rows_affected > 0
    
    def update_workflow_name(self, workflow_id: str, name: str, user_id: str):
        """워크플로우 이름 업데이트"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE workflows 
            SET name = %s, updated_at = CURRENT_TIMESTAMP 
            WHERE workflow_id = %s AND user_id = %s
        ''', (name, workflow_id, user_id))
        
        affected_rows = cursor.rowcount
        conn.commit()
        conn.close()
        
        return affected_rows > 0
    
    def delete_workflow(self, workflow_id: str, user_id: str):
        """워크플로우 삭제"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM workflows 
            WHERE workflow_id = %s AND user_id = %s
        ''', (workflow_id, user_id))
        
        conn.commit()
        conn.close()
    
    def get_workflow_by_id(self, workflow_id: str, user_id: str):
        """워크플로우 ID로 워크플로우 정보 조회"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            SELECT workflow_id, name, status, created_at, updated_at
            FROM workflows 
            WHERE workflow_id = %s AND user_id = %s
        ''', (workflow_id, user_id))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                "workflow_id": result["workflow_id"],
                "name": result["name"],
                "status": result["status"],
                "created_at": result["created_at"],
                "updated_at": result["updated_at"]
            }
        return None
    
    # 프로젝트 메서드
    def create_project(self, user_id: str, name: str):
        """새 프로젝트 생성"""
        project_id = str(uuid.uuid4())
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO projects (project_id, user_id, name)
            VALUES (%s, %s, %s)
        ''', (project_id, user_id, name))
        
        conn.commit()
        conn.close()
        
        return project_id
    
    def get_user_projects(self, user_id: str):
        """사용자의 모든 프로젝트 목록 조회"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            SELECT project_id, name, created_at, updated_at
            FROM projects 
            WHERE user_id = %s
            ORDER BY updated_at DESC
        ''', (user_id,))
        
        rows = cursor.fetchall()
        conn.close()
        
        projects = []
        for row in rows:
            projects.append({
                "project_id": row["project_id"],
                "name": row["name"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            })
        
        return projects
    
    def update_project_name(self, project_id: str, name: str, user_id: str):
        """프로젝트 이름 업데이트"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE projects 
            SET name = %s, updated_at = CURRENT_TIMESTAMP 
            WHERE project_id = %s AND user_id = %s
        ''', (name, project_id, user_id))
        
        affected_rows = cursor.rowcount
        conn.commit()
        conn.close()
        
        return affected_rows > 0
    
    def delete_project(self, project_id: str, user_id: str):
        """프로젝트 삭제 (해당 프로젝트의 워크플로우들은 project_id를 NULL로 설정)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # 프로젝트 소속 워크플로우들의 project_id를 NULL로 설정
        cursor.execute('''
            UPDATE workflows 
            SET project_id = NULL, updated_at = CURRENT_TIMESTAMP 
            WHERE project_id = %s AND user_id = %s
        ''', (project_id, user_id))
        
        # 프로젝트 삭제
        cursor.execute('''
            DELETE FROM projects 
            WHERE project_id = %s AND user_id = %s
        ''', (project_id, user_id))
        
        conn.commit()
        conn.close()
    
    def assign_workflow_to_project(self, workflow_id: str, project_id: str, user_id: str):
        """워크플로우를 프로젝트에 할당"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE workflows 
            SET project_id = %s, updated_at = CURRENT_TIMESTAMP 
            WHERE workflow_id = %s AND user_id = %s
        ''', (project_id, workflow_id, user_id))
        
        affected_rows = cursor.rowcount
        conn.commit()
        conn.close()
        
        return affected_rows > 0

    # 게시판 메서드
    def create_board_post(self, user_id: str, title: str, description: str, workflow_id: str, workflow_name: str):
        """새 게시글 생성"""
        post_id = str(uuid.uuid4())
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO board_posts (post_id, user_id, title, description, workflow_id, workflow_name)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (post_id, user_id, title, description, workflow_id, workflow_name))
        
        conn.commit()
        conn.close()
        
        return post_id
    
    def get_board_posts(self, limit: int = 50, offset: int = 0):
        """게시글 목록 조회 with pagination"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # 전체 게시글 수 조회
        cursor.execute('SELECT COUNT(*) as count FROM board_posts')
        total_count = cursor.fetchone()["count"]
        
        cursor.execute('''
            SELECT bp.post_id, bp.user_id, bp.title, bp.description, bp.workflow_id, 
                   bp.workflow_name, bp.download_count, bp.created_at, bp.updated_at,
                   u.first_name, u.last_name
            FROM board_posts bp
            JOIN users u ON bp.user_id = u.user_id
            ORDER BY bp.created_at DESC
            LIMIT %s OFFSET %s
        ''', (limit, offset))
        
        posts = cursor.fetchall()
        conn.close()
        
        posts_data = [
            {
                "post_id": post["post_id"],
                "user_id": post["user_id"],
                "title": post["title"],
                "description": post["description"],
                "workflow_id": post["workflow_id"],
                "workflow_name": post["workflow_name"],
                "download_count": post["download_count"],
                "created_at": post["created_at"],
                "updated_at": post["updated_at"],
                "author_name": f"{post['first_name']} {post['last_name']}"
            }
            for post in posts
        ]
        
        return {
            "posts": posts_data,
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total_count
        }
    
    def get_board_post_by_id(self, post_id: str):
        """게시글 ID로 조회"""
        conn = self.get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute('''
            SELECT bp.post_id, bp.user_id, bp.title, bp.description, bp.workflow_id, 
                   bp.workflow_name, bp.download_count, bp.created_at, bp.updated_at,
                   u.first_name, u.last_name
            FROM board_posts bp
            JOIN users u ON bp.user_id = u.user_id
            WHERE bp.post_id = %s
        ''', (post_id,))
        
        post = cursor.fetchone()
        conn.close()
        
        if post:
            return {
                "post_id": post["post_id"],
                "user_id": post["user_id"],
                "title": post["title"],
                "description": post["description"],
                "workflow_id": post["workflow_id"],
                "workflow_name": post["workflow_name"],
                "download_count": post["download_count"],
                "created_at": post["created_at"],
                "updated_at": post["updated_at"],
                "author_name": f"{post['first_name']} {post['last_name']}"
            }
        return None
    
    def update_board_post(self, post_id: str, user_id: str, title: str = None, description: str = None, 
                         workflow_id: str = None, workflow_name: str = None):
        """게시글 수정"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        updates = []
        params = []
        
        if title is not None:
            updates.append("title = %s")
            params.append(title)
        if description is not None:
            updates.append("description = %s")
            params.append(description)
        if workflow_id is not None:
            updates.append("workflow_id = %s")
            params.append(workflow_id)
        if workflow_name is not None:
            updates.append("workflow_name = %s")
            params.append(workflow_name)
        
        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.extend([post_id, user_id])
            
            cursor.execute(f'''
                UPDATE board_posts 
                SET {", ".join(updates)}
                WHERE post_id = %s AND user_id = %s
            ''', params)
            
            affected_rows = cursor.rowcount
            conn.commit()
            conn.close()
            
            return affected_rows > 0
        
        conn.close()
        return False
    
    def delete_board_post(self, post_id: str, user_id: str):
        """게시글 삭제"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM board_posts 
            WHERE post_id = %s AND user_id = %s
        ''', (post_id, user_id))
        
        affected_rows = cursor.rowcount
        conn.commit()
        conn.close()
        
        return affected_rows > 0
    
    def increment_download_count(self, post_id: str):
        """다운로드 수 증가"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE board_posts 
            SET download_count = download_count + 1
            WHERE post_id = %s
        ''', (post_id,))
        
        conn.commit()
        conn.close()


# JWT 관련 함수들
def create_access_token(user_data: dict):
    """JWT 토큰 생성"""
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    to_encode = user_data.copy()
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """JWT 토큰 검증"""
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("user_id")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired"
        )
    except jwt.JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

def verify_user_api_key(x_api_key: str = Header(None, alias="X-API-Key")):
    """사용자 API 키 검증"""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API key required")
    
    # 데이터베이스에서 API 키로 사용자 조회
    db = DatabaseManager()
    conn = db.get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute('''
        SELECT user_id, email, first_name, last_name 
        FROM users 
        WHERE api_key = %s
    ''', (x_api_key,))
    
    user = cursor.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "first_name": user["first_name"],
        "last_name": user["last_name"]
    }

class ClaudeMCPBackend:
    def __init__(self):
        self.anthropic = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.mcp_url = os.getenv("MCP_SERVER_URL")
        self.auth_token = os.getenv("MCP_AUTH_TOKEN")
        self.db = DatabaseManager()
        
        # 서버 시작 시 정적 파일들 로드
        self.tools = self._load_tools()
        self.system = self._load_system()
        
    def _load_tools(self):
        """서버 시작 시 툴 목록 로드"""
        tools_file_path = os.path.join(os.path.dirname(__file__), "prompt", "tools", "tools_latest.json")
        with open(tools_file_path, 'r', encoding='utf-8') as f:
            tools = json.load(f)
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] MCP 도구 {len(tools)}개 로드 완료")
        return tools
    
    def _load_system(self):
        """서버 시작 시 시스템 프롬프트 로드"""
        system_file_path = os.path.join(os.path.dirname(__file__), "prompt", "system", "system_latest.json")
        with open(system_file_path, 'r', encoding='utf-8') as f:
            system_prompt = json.load(f)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 시스템 프롬프트 로드 완료")
        return system_prompt
    
    async def call_tool(self, name, args, user_api_key=None):
        # n8n API 툴의 경우 api_key를 자동으로 추가
        if name in ["n8n_create_workflow", "n8n_update_full_workflow", "n8n_delete_workflow", "n8n_list_workflows"] and user_api_key:
            if isinstance(args, dict):
                args["api_key"] = user_api_key
                print(f"[DEBUG] {name} 툴에 API 키 자동 추가됨")
        
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        async with httpx.AsyncClient() as client:
            response = await client.post(self.mcp_url, json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": name, "arguments": args}
            }, headers=headers)
            
            result = response.json().get("result", {})
            raw_content = result.get("content", result)
            
            # MCP 응답에서 text 내용만 추출
            if isinstance(raw_content, list) and len(raw_content) > 0:
                first_block = raw_content[0]
                if isinstance(first_block, dict) and first_block.get("type") == "text":
                    text_content = first_block.get("text", "")
                    
                    # JSON 문자열이면 파싱해서 객체로 반환
                    try:
                        parsed_json = json.loads(text_content)
                        return parsed_json
                    except json.JSONDecodeError:
                        # JSON이 아니면 문자열 그대로 반환
                        return text_content
            
            # 배열이 아니면 그대로 반환
            return str(raw_content)
    
    def add_cache_control_to_messages(self, messages):
        if not messages:
            return
            
        # 모든 메시지를 content 배열 형태로 정규화
        for msg in messages:
            if isinstance(msg.get("content"), str):
                msg["content"] = [{"type": "text", "text": msg["content"]}]
        
        # 기존 cache_control 모두 제거
        for msg in messages:
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and "cache_control" in block:
                        del block["cache_control"]
        
        # 마지막 user 메시지에만 cache_control 추가
        user_messages = [i for i, msg in enumerate(messages) if msg.get("role") == "user"]
        if user_messages:
            last_user_idx = user_messages[-1]
            last_user_msg = messages[last_user_idx]
            if isinstance(last_user_msg.get("content"), list) and last_user_msg["content"]:
                last_block = last_user_msg["content"][-1]
                if isinstance(last_block, dict):
                    last_block["cache_control"] = {"type": "ephemeral"}
    
    async def generate_session_title(self, user_message: str):
        """하이쿠 모델로 사용자 메시지 기반 세션 제목 생성"""
        try:
            response = self.anthropic.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=30,
                system="사용자의 질문을 기반으로 간단하고 명확한 채팅방 제목을 한국어로 생성해주세요. 적절한 길이로 만들어주세요.",
                messages=[{"role": "user", "content": f"다음 질문에 적합한 채팅방 제목을 생성해주세요: {user_message}"}]
            )
            
            title = response.content[0].text.strip()
            title = title.replace('"', '').replace("'", '').strip()
            return title[:50]
        except Exception as e:
            print(f"제목 생성 실패: {e}")
            return user_message[:20] + "..." if len(user_message) > 20 else user_message
    
    async def chat_stream(self, content, user_id: str, session_id: str = None):
        
        # 1. session_id가 없다면 첫번째 텍스트 블록으로 바로 제목 생성 시작, session_id도 만들기
        if not session_id:
            session_id = str(uuid.uuid4())
            # 제목 생성을 위해 첫 번째 텍스트 블록 사용
            title_text = next((block["text"] for block in content if block.get("type") == "text"), "새 채팅")
            title = await self.generate_session_title(title_text)
            self.db.create_session(user_id, title, session_id)
            yield f"data: {json.dumps({'type': 'session_created', 'session_id': session_id, 'title': title})}\n\n"
        
        # 2. messages 배열 만들기
        messages = []
        
        # 3. session_id가 있었다면 과거 데이터 db에서 가져와서 messages에 추가
        if session_id:
            try:
                messages = self.db.get_session_messages(session_id, user_id)
                # 기존 메시지들에 캐시 적용 (마지막 메시지에 캐시)
                if messages:
                    self.add_cache_control_to_messages(messages)
            except HTTPException:
                # 세션이 존재하지 않거나 권한이 없으면 빈 메시지로 처리
                messages = []
        
        # 4. content 배열 messages에 추가
        messages.append({"role": "user", "content": content})
        
        # 5. 새로운 마지막 user 메시지 cache_control
        self.add_cache_control_to_messages(messages)
        
        # 6. session_id로 content 배열 db에 저장
        self.db.save_message(session_id, "user", json.dumps(content, ensure_ascii=False))
        
        # 7. 사용자 정보 가져오기
        current_user = self.db.get_user_by_id(user_id)
        user_api_key = current_user.get('api_key', '') if current_user else ''
        
        # 8. While 문 돌기 (중단 요청이 없으면 계속)
        while session_id not in stop_requests:
            try:
                with self.anthropic.messages.stream(
                    model="claude-sonnet-4-20250514",
                    max_tokens=16000,
                    thinking={"type": "enabled", "budget_tokens": 10000},
                    tools=self.tools,
                    system=self.system,
                    extra_headers={"anthropic-beta": "interleaved-thinking-2025-05-14"},
                    messages=messages
                ) as stream:
                    
                    thinking_text = ""
                    current_block_type = None
                    
                    for event in stream:
                        if event.type == "content_block_start":
                            current_block_type = event.content_block.type
                            if current_block_type == "thinking":
                                yield f"data: {json.dumps({'type': 'thinking_start'})}\n\n"
                            elif current_block_type == "text":
                                yield f"data: {json.dumps({'type': 'text_start'})}\n\n"
                            elif current_block_type == "tool_use":
                                # tool_use 시작 시 input은 빈 객체일 수 있음
                                tool_input = getattr(event.content_block, 'input', {})
                                yield f"data: {json.dumps({'type': 'tool_use_start', 'id': event.content_block.id, 'name': event.content_block.name, 'input': tool_input})}\n\n"
                        
                        elif event.type == "content_block_delta":
                            if event.delta.type == "thinking_delta":
                                thinking_text += event.delta.thinking
                                yield f"data: {json.dumps({'type': 'thinking_delta', 'text': event.delta.thinking})}\n\n"
                                await asyncio.sleep(0)
                            elif event.delta.type == "text_delta":
                                yield f"data: {json.dumps({'type': 'text_delta', 'text': event.delta.text})}\n\n"
                                await asyncio.sleep(0)
                        
                        elif event.type == "content_block_stop":
                            if current_block_type == "thinking":
                                yield f"data: {json.dumps({'type': 'thinking_stop'})}\n\n"
                                thinking_text = ""
                
                    # Usage 로그 출력 및 메시지 추출
                    message_obj = stream.get_final_message()
                    usage = message_obj.usage                
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] USAGE - Input: {usage.input_tokens}, Output: {usage.output_tokens}, Cache Create: {usage.cache_creation_input_tokens}, Cache Read: {usage.cache_read_input_tokens}")
                    
                    # 완성된 tool_use의 input 정보를 스트리밍으로 전송
                    for block in message_obj.content:
                        if hasattr(block, 'type') and block.type == 'tool_use':
                            yield f"data: {json.dumps({'type': 'tool_use_complete', 'id': block.id, 'name': block.name, 'input': block.input})}\n\n"
                    
                    # response_content를 JSON 직렬화 가능한 형태로 변환
                    assistant_content = []
                    for block in message_obj.content:
                        if hasattr(block, 'model_dump'):
                            assistant_content.append(block.model_dump())
                        else:
                            assistant_content.append({"type": "text", "text": str(block)})
                    
                    # 어시스턴트 응답을 데이터베이스에 저장
                    self.db.save_message(session_id, "assistant", json.dumps(assistant_content, ensure_ascii=False))
                    
                    messages.append({"role": "assistant", "content": assistant_content})
                    
                    tool_blocks = [b for b in message_obj.content if b.type == 'tool_use']
                    
                    if not tool_blocks:
                        yield f"data: {json.dumps({'type': 'session_id', 'session_id': session_id})}\n\n"
                        yield f"data: {json.dumps({'type': 'complete'})}\n\n"
                        break
                    
                    # 도구 실행
                    tool_results = []
                    for tool in tool_blocks:
                        yield f"data: {json.dumps({'type': 'tool_execution', 'name': tool.name, 'input': tool.input})}\n\n"
                        
                        try:
                            result = await self.call_tool(tool.name, tool.input, user_api_key)
                            
                            # 결과 검증
                            if result is None:
                                result = {"error": "Tool execution returned no result"}
                            
                            yield f"data: {json.dumps({'type': 'tool_result', 'tool_use_id': tool.id, 'content': result})}\n\n"
                            
                            # Anthropic API는 tool_result content가 문자열이어야 함
                            content_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                            
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool.id,
                                "content": content_str
                            })
                        except Exception as e:
                            error_msg = str(e)
                            yield f"data: {json.dumps({'type': 'tool_error', 'name': tool.name, 'error': error_msg})}\n\n"
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool.id,
                                "content": f"오류: {error_msg}"
                            })
                    
                    # 도구 결과를 DB에 저장하고 메시지에 추가
                    self.db.save_message(session_id, "user", json.dumps(tool_results, ensure_ascii=False))
                    messages.append({"role": "user", "content": tool_results})
                    
                    self.add_cache_control_to_messages(messages)
                        
            except Exception as e:
                error_str = str(e)
                print(f"[ERROR] API 요청 실패: {error_str}")
                if "overloaded_error" in error_str:
                    print(f"[WARNING] API 과부하 - 2초 후 재시도")
                    await asyncio.sleep(2)
                    continue  # 다시 while 루프 시도
                else:
                    print(f"[ERROR] 복구 불가능한 오류 발생: {error_str}")
                    return
        
        # while 루프 종료 - 중단 요청 처리
        if session_id in stop_requests:
            stop_requests.discard(session_id)  # 중단 요청 제거
            print(f"[INFO] 세션 {session_id} 중단 처리 완료")

# 전역 인스턴스
claude_backend = ClaudeMCPBackend()

# n8n API 헬퍼 함수들
async def get_workflow_from_n8n(workflow_id: str):
    """n8n에서 워크플로우 JSON 데이터 가져오기"""
    n8n_url = os.getenv("N8N_API_URL")
    n8n_api_key = os.getenv("N8N_API_KEY")
    
    if not n8n_api_key:
        print("[WARNING] N8N_API_KEY가 설정되지 않았습니다")
        return None
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{n8n_url}/workflows/{workflow_id}",
                headers={
                    "X-N8N-API-KEY": n8n_api_key,
                },
                timeout=10.0
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                print(f"[ERROR] n8n 워크플로우 조회 실패: {response.status_code} - {response.text}")
                return None
                
    except Exception as e:
        print(f"[ERROR] n8n API 호출 중 오류: {str(e)}")
        return None

async def update_workflow_in_n8n(workflow_id: str, name: str):
    """n8n에서 워크플로우 이름 업데이트"""
    n8n_url = os.getenv("N8N_API_URL")
    n8n_api_key = os.getenv("N8N_API_KEY")
    
    if not n8n_api_key:
        print("[WARNING] N8N_API_KEY가 설정되지 않았습니다")
        return False
    
    try:
        async with httpx.AsyncClient() as client:
            # 워크플로우 먼저 가져오기 
            get_response = await client.get(
                f"{n8n_url}/workflows/{workflow_id}",
                headers={
                    "X-N8N-API-KEY": n8n_api_key,
                },
                timeout=10.0
            )
            
            if get_response.status_code != 200:
                print(f"[ERROR] n8n 워크플로우 조회 실패: {get_response.status_code} - {get_response.text}")
                return False
            
            workflow_data = get_response.json()
            
            # name 바꾸고 필요한 필드만 복사
            update_data = {
                "name": name,
                "nodes": workflow_data["nodes"],
                "connections": workflow_data["connections"]
            }
            for field in ["settings", "staticData", "pinData"]:
                if field in workflow_data and workflow_data[field]:
                    update_data[field] = workflow_data[field]
            
            put_response = await client.put(
                f"{n8n_url}/workflows/{workflow_id}",
                headers={
                    "X-N8N-API-KEY": n8n_api_key,
                    "Content-Type": "application/json"
                },
                json=update_data,
                timeout=10.0
            )
            
            if put_response.status_code == 200:
                print(f"[INFO] n8n 워크플로우 이름 업데이트 성공 (PUT): {workflow_id} -> {name}")
                return True
            else:
                print(f"[ERROR] n8n 워크플로우 이름 업데이트 실패: {put_response.status_code} - {put_response.text}")
                return False
                
    except Exception as e:
        print(f"[ERROR] n8n API 호출 중 오류: {e}")
        return False

async def toggle_workflow_in_n8n(workflow_id: str, activate: bool):
    """n8n에서 워크플로우 활성화/비활성화"""
    n8n_url = os.getenv("N8N_API_URL")
    n8n_api_key = os.getenv("N8N_API_KEY")
    
    if not n8n_api_key:
        print("[WARNING] N8N_API_KEY가 설정되지 않았습니다")
        return False
    
    try:
        async with httpx.AsyncClient() as client:
            # 정확한 n8n API 엔드포인트 사용
            if activate:
                endpoint = f"{n8n_url}/workflows/{workflow_id}/activate"
            else:
                endpoint = f"{n8n_url}/workflows/{workflow_id}/deactivate"
            
            response = await client.post(
                endpoint,
                headers={
                    "X-N8N-API-KEY": n8n_api_key,
                    "Content-Type": "application/json"
                },
                timeout=10.0
            )
            
            if response.status_code in [200, 201]:
                action = "활성화" if activate else "비활성화"
                print(f"[INFO] n8n 워크플로우 {action} 성공: {workflow_id}")
                return True
            else:
                action = "활성화" if activate else "비활성화"
                print(f"[ERROR] n8n 워크플로우 {action} 실패: {response.status_code} - {response.text}")
                return False
                
    except Exception as e:
        print(f"[ERROR] n8n API 호출 중 오류: {e}")
        return False

async def delete_workflow_in_n8n(workflow_id: str):
    """n8n에서 워크플로우 삭제"""
    n8n_url = os.getenv("N8N_API_URL")
    n8n_api_key = os.getenv("N8N_API_KEY")
    
    if not n8n_api_key:
        print("[WARNING] N8N_API_KEY가 설정되지 않았습니다")
        return False
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{n8n_url}/workflows/{workflow_id}",
                headers={
                    "X-N8N-API-KEY": n8n_api_key
                },
                timeout=10.0
            )
            
            if response.status_code == 200:
                print(f"[INFO] n8n 워크플로우 삭제 성공: {workflow_id}")
                return True
            else:
                print(f"[ERROR] n8n 워크플로우 삭제 실패: {response.status_code} - {response.text}")
                return False
                
    except Exception as e:
        print(f"[ERROR] n8n API 호출 중 오류: {e}")
        return False

# 인증 관련 엔드포인트
@app.post("/register", response_model=LoginResponse)
async def register(user: UserRegister):
    """회원가입"""
    user_id = claude_backend.db.create_user(
        user.email, user.first_name, user.last_name, user.password,
        user.marketing_consent, user.analytics_consent
    )
    
    user_data = UserResponse(
        user_id=user_id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name
    )
    
    token = create_access_token(user_data.model_dump())
    
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        user=user_data
    )

@app.post("/login", response_model=LoginResponse)
async def login(user: UserLogin):
    """로그인"""
    user_data = claude_backend.db.authenticate_user(user.email, user.password)
    
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 잘못되었습니다"
        )
    
    token = create_access_token(user_data)
    
    user_response = UserResponse(**user_data)
    
    print(f"사용자 {user.email}의 백엔드 로그인 성공")
    
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        user=user_response
    )

# 채팅 관련 엔드포인트
@app.post("/chat")
async def chat(request: ChatRequest, current_user: dict = Depends(verify_token)):
    """통합 채팅 (텍스트 + 파일)"""
    try:
        # 프론트엔드에서 받은 content를 그대로 처리
        # content는 이미 [{"type": "text", "text": "..."}, {"type": "image", "source": {...}}, ...] 형태
        
        return StreamingResponse(
            claude_backend.chat_stream(request.content, current_user["user_id"], request.session_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/chat/stop/{session_id}")
async def stop_chat(session_id: str, current_user: dict = Depends(verify_token)):
    """채팅 중단 요청"""
    try:
        # 중단 요청 추가
        stop_requests.add(session_id)
        return {"message": "Stop request received", "session_id": session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 사용자 프로필 관련 엔드포인트
@app.get("/user/profile")
async def get_user_profile(current_user: dict = Depends(verify_token)):
    """사용자 프로필 조회 (동의 정보 포함)"""
    try:
        profile = claude_backend.db.get_user_profile(current_user["user_id"])
        return profile
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/user/consent")
async def update_user_consent(consent: UserConsent, current_user: dict = Depends(verify_token)):
    """사용자 동의 정보 업데이트"""
    try:
        result = claude_backend.db.update_user_consent(
            current_user["user_id"],
            consent.marketing_consent,
            consent.analytics_consent
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/sessions")
async def get_user_sessions(current_user: dict = Depends(verify_token)):
    """사용자의 세션 목록 조회"""
    sessions = claude_backend.db.get_user_sessions(current_user["user_id"])
    return {"sessions": sessions}

@app.get("/sessions/{session_id}")
async def get_session_info(session_id: str, current_user: dict = Depends(verify_token)):
    """세션 정보 및 메시지 조회"""
    try:
        # 프론트엔드용 메시지 조회 (메타데이터 제거된 버전)
        messages = claude_backend.db.get_session_messages_for_frontend(session_id, current_user["user_id"])
        
        # 세션 정보 조회
        sessions = claude_backend.db.get_user_sessions(current_user["user_id"])
        session = next((s for s in sessions if s["session_id"] == session_id), None)
        if not session:
            raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")
        
        # 세션 정보와 메시지를 함께 반환
        return {
            "session_id": session["session_id"],
            "title": session["title"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "messages": messages
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다")

@app.put("/sessions/{session_id}")
async def update_session(session_id: str, session_update: SessionTitleUpdate, current_user: dict = Depends(verify_token)):
    """세션 제목 업데이트"""
    claude_backend.db.update_session_title(session_id, session_update.title, current_user["user_id"])
    return {"session_id": session_id, "title": session_update.title}

@app.delete("/sessions/{session_id}", response_model=MessageResponse)
async def delete_session(session_id: str, current_user: dict = Depends(verify_token)):
    """세션 삭제"""
    claude_backend.db.delete_session(session_id, current_user["user_id"])
    return MessageResponse(message="세션이 삭제되었습니다")

# Public API - API 키 기반 워크플로우 엔드포인트 (MCP 서버용)
@app.post("/api/workflows")
async def create_workflow_with_api_key(workflow: ApiKeyWorkflowCreate, current_user: dict = Depends(verify_user_api_key)):
    """API 키 기반: 워크플로우 생성 (MCP 서버용)"""
    print(f"[DEBUG] MCP 서버로부터 워크플로우 등록 요청: workflow_id={workflow.workflow_id}, name={workflow.name}, user_id={current_user['user_id']}")
    try:
        workflow_id = claude_backend.db.create_workflow(
            current_user["user_id"], workflow.workflow_id, workflow.name
        )
        print(f"[DEBUG] 워크플로우 등록 성공: {workflow_id}")
        return {"workflow_id": workflow_id, "user_id": current_user["user_id"]}
    except psycopg2.IntegrityError:
        print(f"[DEBUG] 워크플로우 등록 실패: 이미 등록된 워크플로우")
        raise HTTPException(status_code=400, detail="이미 등록된 워크플로우입니다")

@app.get("/api/workflows")
async def get_user_workflows_api(current_user: dict = Depends(verify_user_api_key)):
    """API 키 기반: 사용자의 워크플로우 목록 조회 (MCP 서버용) - workflow_id, title만 반환"""
    workflows = claude_backend.db.get_user_workflows_simple(current_user["user_id"])
    return {"workflows": workflows}

@app.post("/api/workflows/verify")
async def verify_workflow_ownership_api(workflow: ApiKeyWorkflowVerify, current_user: dict = Depends(verify_user_api_key)):
    """API 키 기반: 워크플로우 소유권 확인 (MCP 서버용)"""
    print(f"[DEBUG] MCP 서버로부터 워크플로우 소유권 확인 요청: workflow_id={workflow.workflow_id}, user_id={current_user['user_id']}")
    workflow_exists = claude_backend.db.get_workflow_by_id(workflow.workflow_id, current_user["user_id"])
    verified = workflow_exists is not None
    print(f"[DEBUG] 워크플로우 소유권 확인 결과: {verified}")
    return {"verified": verified}

@app.post("/api/workflows/delete")
async def delete_workflow_with_verification_api(workflow: ApiKeyWorkflowDelete, current_user: dict = Depends(verify_user_api_key)):
    """API 키 기반: 워크플로우 소유권 확인 후 삭제 (MCP 서버용)"""
    print(f"[DEBUG] MCP 서버로부터 워크플로우 삭제 요청: workflow_id={workflow.workflow_id}, user_id={current_user['user_id']}")
    workflow_exists = claude_backend.db.get_workflow_by_id(workflow.workflow_id, current_user["user_id"])
    if not workflow_exists:
        print(f"[DEBUG] 워크플로우 삭제 실패: 권한 없음 또는 존재하지 않음")
        return {"deleted": False, "error": "워크플로우를 찾을 수 없거나 권한이 없습니다"}
    
    claude_backend.db.delete_workflow(workflow.workflow_id, current_user["user_id"])
    print(f"[DEBUG] 워크플로우 삭제 성공: {workflow.workflow_id}")
    return {"deleted": True}

# 내부 API - JWT 토큰 기반 워크플로우 엔드포인트
@app.get("/workflows")
async def get_user_workflows(current_user: dict = Depends(verify_token)):
    """사용자의 워크플로우 목록 조회"""
    workflows = claude_backend.db.get_user_workflows(current_user["user_id"])
    return {"workflows": workflows}

@app.get("/workflows/{workflow_id}/json")
async def get_workflow_json(workflow_id: str, current_user: dict = Depends(verify_token)):
    """내 워크플로우의 JSON 데이터 가져오기"""
    try:
        # 워크플로우 소유권 확인
        workflow = claude_backend.db.get_workflow_by_id(workflow_id, current_user["user_id"])
        if not workflow:
            raise HTTPException(status_code=404, detail="워크플로우를 찾을 수 없습니다")
        
        # n8n에서 워크플로우 데이터 가져오기
        workflow_data = await get_workflow_from_n8n(workflow_id)
        
        if workflow_data is None:
            raise HTTPException(status_code=404, detail="n8n에서 워크플로우를 찾을 수 없습니다")
        
        return workflow_data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"워크플로우 데이터 조회 실패: {str(e)}")

@app.delete("/workflows/{workflow_id}", response_model=MessageResponse)
async def delete_workflow(workflow_id: str, current_user: dict = Depends(verify_token)):
    """워크플로우 삭제 (n8n과 백엔드 DB 모두에서 삭제)"""
    # 워크플로우 존재 확인
    existing_workflow = claude_backend.db.get_workflow_by_id(workflow_id, current_user["user_id"])
    if not existing_workflow:
        raise HTTPException(status_code=404, detail="워크플로우를 찾을 수 없습니다")
    
    # 먼저 n8n에서 워크플로우 삭제
    n8n_success = await delete_workflow_in_n8n(workflow_id)
    if not n8n_success:
        raise HTTPException(status_code=500, detail="n8n에서 워크플로우 삭제에 실패했습니다")
    
    # n8n 성공 시 백엔드 DB에서도 삭제
    claude_backend.db.delete_workflow(workflow_id, current_user["user_id"])
    return MessageResponse(message="워크플로우가 삭제되었습니다")

@app.put("/workflows/{workflow_id}/name")
async def update_workflow_name(workflow_id: str, workflow_update: WorkflowNameUpdate, current_user: dict = Depends(verify_token)):
    """워크플로우 이름 변경 (n8n과 백엔드 DB 모두 업데이트)"""
    # 먼저 n8n에서 워크플로우 이름 변경
    n8n_success = await update_workflow_in_n8n(workflow_id, workflow_update.name)
    if not n8n_success:
        raise HTTPException(status_code=500, detail="n8n에서 워크플로우 이름 변경에 실패했습니다")
    
    # n8n 성공 시 백엔드 DB도 업데이트
    db_success = claude_backend.db.update_workflow_name(workflow_id, workflow_update.name, current_user["user_id"])
    if not db_success:
        raise HTTPException(status_code=404, detail="워크플로우를 찾을 수 없습니다")
    
    return MessageResponse(message=f"워크플로우 이름이 '{workflow_update.name}'으로 변경되었습니다")

@app.post("/workflows/{workflow_id}/toggle", response_model=MessageResponse)
async def toggle_workflow_status(workflow_id: str, current_user: dict = Depends(verify_token)):
    """워크플로우 활성화/비활성화 토글"""
    # 현재 상태 조회 (디버깅 추가)
    workflow = claude_backend.db.get_workflow_by_id(workflow_id, current_user["user_id"])
    print(f"[DEBUG] 워크플로우 조회 결과: {workflow}")
    print(f"[DEBUG] 사용자 ID: {current_user['user_id']}")
    print(f"[DEBUG] 워크플로우 ID: {workflow_id}")
    
    if not workflow:
        raise HTTPException(status_code=404, detail="워크플로우를 찾을 수 없습니다")
    
    # 현재 상태의 반대로 토글
    new_status = "inactive" if workflow["status"] == "active" else "active"
    activate = new_status == "active"
    
    # n8n에서 상태 변경
    n8n_success = await toggle_workflow_in_n8n(workflow_id, activate)
    if not n8n_success:
        raise HTTPException(status_code=500, detail="n8n에서 워크플로우 상태 변경에 실패했습니다")
    
    # 백엔드 DB에서 상태 업데이트
    db_success = claude_backend.db.update_workflow_status(workflow_id, new_status, current_user["user_id"])
    if not db_success:
        raise HTTPException(status_code=404, detail="워크플로우를 찾을 수 없습니다")
    
    action = "활성화" if activate else "비활성화"
    return MessageResponse(message=f"워크플로우가 {action}되었습니다")

# 프로젝트 관리 엔드포인트들
@app.post("/projects")
async def create_project(project: ProjectCreate, current_user: dict = Depends(verify_token)):
    """프로젝트 생성"""
    project_id = claude_backend.db.create_project(current_user["user_id"], project.name)
    return {"project_id": project_id, "name": project.name}

@app.get("/projects")
async def get_user_projects(current_user: dict = Depends(verify_token)):
    """사용자의 프로젝트 목록 조회"""
    projects = claude_backend.db.get_user_projects(current_user["user_id"])
    return {"projects": projects}

@app.put("/projects/{project_id}", response_model=MessageResponse)
async def update_project(project_id: str, project_update: ProjectUpdate, current_user: dict = Depends(verify_token)):
    """프로젝트 이름 변경"""
    success = claude_backend.db.update_project_name(project_id, project_update.name, current_user["user_id"])
    if not success:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다")
    
    return MessageResponse(message=f"프로젝트 이름이 '{project_update.name}'으로 변경되었습니다")

@app.delete("/projects/{project_id}", response_model=MessageResponse)
async def delete_project(project_id: str, current_user: dict = Depends(verify_token)):
    """프로젝트 삭제"""
    claude_backend.db.delete_project(project_id, current_user["user_id"])
    return MessageResponse(message="프로젝트가 삭제되었습니다")

@app.put("/workflows/{workflow_id}/project", response_model=MessageResponse)
async def assign_workflow_to_project(workflow_id: str, workflow_update: WorkflowProjectUpdate, current_user: dict = Depends(verify_token)):
    """워크플로우를 프로젝트에 할당 (project_id가 None이면 프로젝트에서 제거)"""
    success = claude_backend.db.assign_workflow_to_project(workflow_id, workflow_update.project_id, current_user["user_id"])
    if not success:
        raise HTTPException(status_code=404, detail="워크플로우를 찾을 수 없습니다")
    
    if workflow_update.project_id:
        return MessageResponse(message="워크플로우가 프로젝트에 할당되었습니다")
    else:
        return MessageResponse(message="워크플로우가 프로젝트에서 제거되었습니다")

# 게시판 API 엔드포인트들
@app.post("/board/posts")
async def create_board_post(post: BoardPostCreate, current_user: dict = Depends(verify_token)):
    """새 게시글 생성"""
    try:
        post_id = claude_backend.db.create_board_post(
            user_id=current_user["user_id"],
            title=post.title,
            description=post.description,
            workflow_id=post.workflow_id,
            workflow_name=post.workflow_name
        )
        return {"post_id": post_id, "message": "게시글이 성공적으로 생성되었습니다"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"게시글 생성 실패: {str(e)}")

@app.get("/board/posts")
async def get_board_posts(limit: int = 50, offset: int = 0):
    """게시글 목록 조회 with pagination"""
    try:
        result = claude_backend.db.get_board_posts(limit=limit, offset=offset)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"게시글 조회 실패: {str(e)}")

@app.get("/board/posts/{post_id}")
async def get_board_post(post_id: str):
    """특정 게시글 조회"""
    try:
        post = claude_backend.db.get_board_post_by_id(post_id)
        if not post:
            raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다")
        return post
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"게시글 조회 실패: {str(e)}")

@app.put("/board/posts/{post_id}", response_model=MessageResponse)
async def update_board_post(post_id: str, post_update: BoardPostUpdate, current_user: dict = Depends(verify_token)):
    """게시글 수정"""
    try:
        success = claude_backend.db.update_board_post(
            post_id=post_id,
            user_id=current_user["user_id"],
            title=post_update.title,
            description=post_update.description,
            workflow_id=post_update.workflow_id,
            workflow_name=post_update.workflow_name
        )
        
        if not success:
            raise HTTPException(status_code=404, detail="게시글을 찾을 수 없거나 수정 권한이 없습니다")
        
        return MessageResponse(message="게시글이 성공적으로 수정되었습니다")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"게시글 수정 실패: {str(e)}")

@app.delete("/board/posts/{post_id}", response_model=MessageResponse)
async def delete_board_post(post_id: str, current_user: dict = Depends(verify_token)):
    """게시글 삭제"""
    try:
        success = claude_backend.db.delete_board_post(post_id, current_user["user_id"])
        if not success:
            raise HTTPException(status_code=404, detail="게시글을 찾을 수 없거나 삭제 권한이 없습니다")
        
        return MessageResponse(message="게시글이 성공적으로 삭제되었습니다")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"게시글 삭제 실패: {str(e)}")

@app.get("/board/posts/{post_id}/download")
async def download_workflow_json(post_id: str, current_user: dict = Depends(verify_token)):
    """n8n에서 워크플로우 JSON 데이터 가져오기 (다운로드 수 증가)"""
    try:
        # 게시글 조회
        post = claude_backend.db.get_board_post_by_id(post_id)
        if not post:
            raise HTTPException(status_code=404, detail="게시글을 찾을 수 없습니다")
        
        # n8n에서 워크플로우 가져오기
        workflow_id = post["workflow_id"]
        
        # n8n에서 워크플로우 데이터 가져오기
        workflow_data = await get_workflow_from_n8n(workflow_id)
        
        if workflow_data is None:
            raise HTTPException(status_code=404, detail="n8n에서 워크플로우를 찾을 수 없습니다")
        
        # 다운로드 수 증가
        claude_backend.db.increment_download_count(post_id)
        
        return {
            "workflow_name": post["workflow_name"],
            "workflow_json": workflow_data,
            "download_count": post["download_count"] + 1
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"워크플로우 다운로드 실패: {str(e)}")




if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)