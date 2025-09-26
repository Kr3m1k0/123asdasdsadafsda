from fastapi import FastAPI, HTTPException, Depends, Form, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlmodel import SQLModel, create_engine, Session, select, Field, Relationship
from typing import List, Optional
from datetime import datetime, timedelta
import json
import secrets
from passlib.context import CryptContext
from jose import JWTError, jwt
import redis
from pydantic import BaseModel
import uvicorn
import os
import httpx

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///betting_system.db")
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "admin-secure-token-change-in-production")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# Discord webhook конфигурация
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "http://localhost:5001/webhook/verify")
DISCORD_WEBHOOK_SECRET = os.getenv("DISCORD_WEBHOOK_SECRET", "ABOBAROFLINT228ZXC")

try:
    redis_client = redis.from_url(REDIS_URL)
    redis_client.ping()
    REDIS_AVAILABLE = True
except:
    REDIS_AVAILABLE = False
    print("Redis недоступен. Защита от брутфорса отключена.")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()


# Модели остаются те же самые...
class BetOption(BaseModel):
    name: str
    coefficient: float


class Bet(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: Optional[str] = None
    options: str = Field(default="[]")
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    winning_option: Optional[str] = None
    user_bets: List["UserBet"] = Relationship(back_populates="bet")

    def get_options(self) -> List[BetOption]:
        try:
            options_data = json.loads(self.options)
            return [BetOption(**option) for option in options_data]
        except:
            return []

    def set_options(self, options: List[BetOption]):
        self.options = json.dumps([option.dict() for option in options])


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True)
    email: str = Field(unique=True)
    discord_id: Optional[str] = None  # Добавлено для связи с Discord
    hashed_password: str
    points: float = Field(default=1000.0)
    is_active: bool = Field(default=True)
    is_verified: bool = Field(default=False)  # Discord верификация
    created_at: datetime = Field(default_factory=datetime.now)
    user_bets: List["UserBet"] = Relationship(back_populates="user")


class UserBet(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    bet_id: int = Field(foreign_key="bet.id")
    selected_option: str
    amount: float
    potential_win: float
    is_won: Optional[bool] = None
    created_at: datetime = Field(default_factory=datetime.now)
    user: User = Relationship(back_populates="user_bets")
    bet: Bet = Relationship(back_populates="user_bets")


class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    discord_id: Optional[str] = None


class UserLogin(BaseModel):
    username: str
    password: str


class BetCreate(BaseModel):
    title: str
    description: Optional[str] = None
    options: List[BetOption]
    end_time: Optional[datetime] = None


class BetUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    options: Optional[List[BetOption]] = None
    is_active: Optional[bool] = None
    end_time: Optional[datetime] = None


class UserBetCreate(BaseModel):
    bet_id: int
    selected_option: str
    amount: float


class BetComplete(BaseModel):
    bet_id: int
    winning_option: str


class UserRating(BaseModel):
    username: str
    points: float
    rank: int


# Discord верификация модели
class DiscordVerification(BaseModel):
    user_id: str  # Discord ID
    username: str  # Имя пользователя в системе ставок
    key: str


class DiscordWebhookData(BaseModel):
    discord_id: str
    key: str
    role_type: str = "member"
    secret: str


# Создание базы данных
engine = create_engine(DATABASE_URL)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


# Утилиты для безопасности (остаются те же)
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def check_rate_limit(ip: str, max_attempts: int = 5, window: int = 300) -> bool:
    if not REDIS_AVAILABLE:
        return True

    key = f"rate_limit:{ip}"
    current = redis_client.get(key)

    if current is None:
        redis_client.setex(key, window, 1)
        return True

    if int(current) >= max_attempts:
        return False

    redis_client.incr(key)
    return True


def reset_rate_limit(ip: str):
    if REDIS_AVAILABLE:
        key = f"rate_limit:{ip}"
        redis_client.delete(key)


# Зависимости для аутентификации (остаются те же)
async def get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(security),
        session: Session = Depends(get_session)
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        raise credentials_exception
    return user


async def verify_admin_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token"
        )
    return True


# FastAPI приложение
app = FastAPI(title="Betting System API", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене указать конкретные домены
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключаем статические файлы (фронтенд)
if os.path.exists("/var/www/betting-system/frontend"):
    app.mount("/static", StaticFiles(directory="/var/www/betting-system/frontend"), name="static")


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


# Health check
@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now()}


# Главная страница
@app.get("/")
async def read_index():
    if os.path.exists("/var/www/betting-system/frontend/index.html"):
        return FileResponse('/var/www/betting-system/frontend/index.html')
    return {"message": "API is running"}


# Discord интеграция endpoints
@app.post("/discord/link", response_model=dict)
async def link_discord_account(
        verification_data: DiscordVerification,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_session)
):
    """Связать аккаунт с Discord"""
    # Проверяем, не связан ли уже этот Discord ID
    existing = session.exec(
        select(User).where(User.discord_id == verification_data.user_id)
    ).first()

    if existing and existing.id != current_user.id:
        raise HTTPException(status_code=400, detail="Discord account already linked to another user")

    # Отправляем запрос к Discord боту для верификации
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                DISCORD_WEBHOOK_URL,
                json={
                    "discord_id": verification_data.user_id,
                    "key": verification_data.key,
                    "role_type": "member",
                    "secret": DISCORD_WEBHOOK_SECRET
                }
            )

            if response.status_code == 200:
                # Обновляем пользователя
                current_user.discord_id = verification_data.user_id
                current_user.is_verified = True
                current_user.points += 500  # Бонус за верификацию

                session.add(current_user)
                session.commit()

                return {
                    "message": "Discord account linked successfully",
                    "bonus_points": 500,
                    "total_points": current_user.points
                }
            else:
                raise HTTPException(status_code=400, detail="Discord verification failed")

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to verify with Discord bot: {str(e)}")


@app.post("/webhook/discord-verified", response_model=dict)
async def discord_verified_webhook(
        data: DiscordWebhookData,
        session: Session = Depends(get_session)
):
    """Webhook endpoint для Discord бота"""
    # Проверяем секретный ключ
    if data.secret != DISCORD_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Находим пользователя по Discord ID
    user = session.exec(
        select(User).where(User.discord_id == data.discord_id)
    ).first()

    if user:
        user.is_verified = True
        session.add(user)
        session.commit()
        return {"message": "User verified", "user_id": user.id}

    return {"message": "User not found", "discord_id": data.discord_id}


# Пользовательские эндпоинты (остаются те же, что были в оригинале)
@app.post("/register", response_model=dict)
async def register_user(
        user_data: UserCreate,
        session: Session = Depends(get_session)
):
    existing_user = session.exec(
        select(User).where(
            (User.username == user_data.username) |
            (User.email == user_data.email)
        )
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=400,
            detail="Username or email already registered"
        )

    hashed_password = get_password_hash(user_data.password)
    user = User(
        username=user_data.username,
        email=user_data.email,
        discord_id=user_data.discord_id,
        hashed_password=hashed_password
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    return {"message": "User registered successfully", "user_id": user.id}


@app.post("/login", response_model=dict)
async def login_user(
        user_data: UserLogin,
        request: Request,
        session: Session = Depends(get_session)
):
    ip = request.client.host
    if not check_rate_limit(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Please try again later."
        )

    user = session.exec(select(User).where(User.username == user_data.username)).first()

    if not user or not verify_password(user_data.password, user.hashed_password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password"
        )

    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")

    reset_rate_limit(ip)

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
        "points": user.points,
        "is_verified": user.is_verified
    }


@app.get("/profile", response_model=dict)
async def get_user_profile(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "discord_id": current_user.discord_id,
        "points": current_user.points,
        "is_verified": current_user.is_verified,
        "created_at": current_user.created_at
    }


# Остальные эндпоинты остаются без изменений...
@app.get("/bets", response_model=List[dict])
async def get_active_bets(session: Session = Depends(get_session)):
    bets = session.exec(select(Bet).where(Bet.is_active == True)).all()
    result = []

    for bet in bets:
        bet_dict = {
            "id": bet.id,
            "title": bet.title,
            "description": bet.description,
            "options": bet.get_options(),
            "created_at": bet.created_at,
            "end_time": bet.end_time,
            "is_active": bet.is_active
        }
        result.append(bet_dict)

    return result


@app.post("/place_bet", response_model=dict)
async def place_bet(
        bet_data: UserBetCreate,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_session)
):
    # Проверяем, что пользователь верифицирован
    if not current_user.is_verified:
        raise HTTPException(
            status_code=403,
            detail="Please verify your Discord account to place bets"
        )

    bet = session.get(Bet, bet_data.bet_id)
    if not bet or not bet.is_active:
        raise HTTPException(status_code=404, detail="Bet not found or inactive")

    if bet.end_time and datetime.now() > bet.end_time:
        raise HTTPException(status_code=400, detail="Betting period has ended")

    if current_user.points < bet_data.amount:
        raise HTTPException(status_code=400, detail="Insufficient points")

    options = bet.get_options()
    selected_option_data = None
    for option in options:
        if option.name == bet_data.selected_option:
            selected_option_data = option
            break

    if not selected_option_data:
        raise HTTPException(status_code=400, detail="Invalid bet option")

    potential_win = bet_data.amount * selected_option_data.coefficient
    user_bet = UserBet(
        user_id=current_user.id,
        bet_id=bet_data.bet_id,
        selected_option=bet_data.selected_option,
        amount=bet_data.amount,
        potential_win=potential_win
    )

    current_user.points -= bet_data.amount

    session.add(user_bet)
    session.add(current_user)
    session.commit()
    session.refresh(user_bet)

    return {
        "message": "Bet placed successfully",
        "bet_id": user_bet.id,
        "potential_win": potential_win,
        "remaining_points": current_user.points
    }


@app.get("/my_bets", response_model=List[dict])
async def get_user_bets(
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_session)
):
    user_bets = session.exec(
        select(UserBet).where(UserBet.user_id == current_user.id)
    ).all()

    result = []
    for user_bet in user_bets:
        bet_info = session.get(Bet, user_bet.bet_id)
        bet_dict = {
            "id": user_bet.id,
            "bet_title": bet_info.title if bet_info else "Unknown",
            "selected_option": user_bet.selected_option,
            "amount": user_bet.amount,
            "potential_win": user_bet.potential_win,
            "is_won": user_bet.is_won,
            "created_at": user_bet.created_at
        }
        result.append(bet_dict)

    return result


@app.get("/leaderboard", response_model=List[UserRating])
async def get_leaderboard(
        limit: int = 10,
        session: Session = Depends(get_session)
):
    users = session.exec(
        select(User).where(User.is_active == True).order_by(User.points.desc()).limit(limit)
    ).all()

    leaderboard = []
    for rank, user in enumerate(users, 1):
        leaderboard.append(UserRating(
            username=user.username,
            points=user.points,
            rank=rank
        ))

    return leaderboard


# Админские эндпоинты (остаются те же)
@app.post("/admin/create_bet", response_model=dict, dependencies=[Depends(verify_admin_token)])
async def create_bet(
        bet_data: BetCreate,
        session: Session = Depends(get_session)
):
    bet = Bet(
        title=bet_data.title,
        description=bet_data.description,
        end_time=bet_data.end_time
    )
    bet.set_options(bet_data.options)

    session.add(bet)
    session.commit()
    session.refresh(bet)

    return {"message": "Bet created successfully", "bet_id": bet.id}


@app.put("/admin/update_bet/{bet_id}", response_model=dict, dependencies=[Depends(verify_admin_token)])
async def update_bet(
        bet_id: int,
        bet_data: BetUpdate,
        session: Session = Depends(get_session)
):
    bet = session.get(Bet, bet_id)
    if not bet:
        raise HTTPException(status_code=404, detail="Bet not found")

    if bet_data.title is not None:
        bet.title = bet_data.title
    if bet_data.description is not None:
        bet.description = bet_data.description
    if bet_data.options is not None:
        bet.set_options(bet_data.options)
    if bet_data.is_active is not None:
        bet.is_active = bet_data.is_active
    if bet_data.end_time is not None:
        bet.end_time = bet_data.end_time

    session.add(bet)
    session.commit()

    return {"message": "Bet updated successfully"}


@app.post("/admin/complete_bet", response_model=dict, dependencies=[Depends(verify_admin_token)])
async def complete_bet(
        completion_data: BetComplete,
        session: Session = Depends(get_session)
):
    bet = session.get(Bet, completion_data.bet_id)
    if not bet:
        raise HTTPException(status_code=404, detail="Bet not found")

    bet.winning_option = completion_data.winning_option
    bet.is_active = False

    user_bets = session.exec(select(UserBet).where(UserBet.bet_id == completion_data.bet_id)).all()

    winners_count = 0
    total_winnings = 0

    for user_bet in user_bets:
        if user_bet.selected_option == completion_data.winning_option:
            user_bet.is_won = True
            user = session.get(User, user_bet.user_id)
            if user:
                user.points += user_bet.potential_win
                session.add(user)
            winners_count += 1
            total_winnings += user_bet.potential_win
        else:
            user_bet.is_won = False

        session.add(user_bet)

    session.add(bet)
    session.commit()

    return {
        "message": "Bet completed successfully",
        "winners_count": winners_count,
        "total_winnings": total_winnings
    }


@app.get("/admin/all_bets", response_model=List[dict], dependencies=[Depends(verify_admin_token)])
async def get_all_bets(session: Session = Depends(get_session)):
    bets = session.exec(select(Bet)).all()
    result = []

    for bet in bets:
        bet_dict = {
            "id": bet.id,
            "title": bet.title,
            "description": bet.description,
            "options": bet.get_options(),
            "is_active": bet.is_active,
            "created_at": bet.created_at,
            "end_time": bet.end_time,
            "winning_option": bet.winning_option
        }
        result.append(bet_dict)

    return result


@app.get("/admin/users", response_model=List[dict], dependencies=[Depends(verify_admin_token)])
async def get_all_users(session: Session = Depends(get_session)):
    users = session.exec(select(User)).all()
    result = []

    for user in users:
        user_dict = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "discord_id": user.discord_id,
            "points": user.points,
            "is_active": user.is_active,
            "is_verified": user.is_verified,
            "created_at": user.created_at
        }
        result.append(user_dict)

    return result


@app.put("/admin/update_user_points/{user_id}", response_model=dict, dependencies=[Depends(verify_admin_token)])
async def update_user_points(
        user_id: int,
        points: float = Form(...),
        session: Session = Depends(get_session)
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.points = points
    session.add(user)
    session.commit()

    return {"message": f"User points updated to {points}"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)