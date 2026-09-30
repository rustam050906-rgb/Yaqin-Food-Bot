"""
Yaqin Food — backend API.

Роли:
  admin — полный доступ: каталог, пользователи, все заказы (принять/отклонить позиции)
  user  — доступ только своему заведению: каталог (просмотр), свои заказы

Доступ выдаётся ТОЛЬКО тем, кого админ добавил по Telegram ID (см. /api/admin/users).
Проверка личности идёт через Telegram WebApp initData (подпись проверяется по BOT_TOKEN),
поэтому подделать чужой доступ через URL нельзя.

Запуск локально:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

Деплой — см. README.md в корне проекта.
"""

import os
import json
import hmac
import base64
import hashlib
from io import BytesIO
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl
from typing import List, Optional

import httpx
from fastapi import FastAPI, Depends, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse, Response
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, ForeignKey, DateTime, Text, Float
)
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker, relationship, backref, declarative_base, Session
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ (задаётся переменными окружения — на Render это вкладка Environment)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./yaqin.db")
ADMIN_TELEGRAM_IDS = [x.strip() for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()]
ADMIN_WEBAPP_URL = os.environ.get("ADMIN_WEBAPP_URL", "")  # ссылка на webapp, для кнопки "Открыть заказ" в уведомлении


def as_utc(dt):
    """Все datetime в БД пишутся как datetime.utcnow() (UTC), но хранятся "наивными"
    (без пометки часового пояса). Перед отдачей клиенту явно помечаем их как UTC,
    чтобы браузер сам правильно пересчитал время в часовой пояс пользователя."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # Render/Neon отдают строку вида postgres://... — SQLAlchemy 2.x хочет postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    if DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# ---------------------------------------------------------------------------
# МОДЕЛИ БД
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, default="")
    restaurant = Column(String, default="")       # заведение
    position = Column(String, default="")          # кухня / бар / гостиница / ...
    role = Column(String, default="user")           # admin | user
    is_active = Column(Boolean, default=True)
    credit_limit = Column(Float, default=0)          # кредитный лимит, выдаётся админом
    credit_balance = Column(Float, default=0)        # текущий остаток кредита
    created_at = Column(DateTime, default=datetime.utcnow)


class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    icon = Column(Text, default="")   # emoji ИЛИ data:image/...;base64,... (фото, загруженное админом)
    sort_order = Column(Integer, default=0)
    parent_id = Column(Integer, ForeignKey("categories.id"), nullable=True)  # NULL = категория верхнего уровня, иначе — подкатегория
    children = relationship("Category", backref=backref("parent", remote_side=[id]))


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey("categories.id"))
    name = Column(String, nullable=False)
    unit = Column(String, default="кг")
    price = Column(Float, default=0)
    old_price = Column(Float, nullable=True)          # если задана и больше price — показываем скидку
    icon = Column(Text, default="")
    in_stock = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    category = relationship("Category")


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    status = Column(String, default="new")          # new | processing | done | cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
    delivery_time = Column(DateTime, nullable=True)
    comment = Column(Text, default="")
    payment_method = Column(String, default="")       # cash | card | credit
    delivery_zone_id = Column(Integer, ForeignKey("delivery_zones.id"), nullable=True)
    delivery_cost = Column(Float, default=0)
    promo_code = Column(String, default="")
    discount_amount = Column(Float, default=0)
    credit_reserved = Column(Float, default=0)   # сумма, списанная с кредитного лимита клиента (payment_method="credit")
    user = relationship("User")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    delivery_zone = relationship("DeliveryZone")


class DeliveryZone(Base):
    __tablename__ = "delivery_zones"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    cost = Column(Float, default=0)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)


class PromoCode(Base):
    __tablename__ = "promo_codes"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)
    discount_percent = Column(Float, nullable=True)
    discount_amount = Column(Float, nullable=True)
    first_order_only = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    max_uses = Column(Integer, nullable=True)
    used_count = Column(Integer, default=0)


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    product_name = Column(String)
    unit = Column(String, default="кг")
    qty = Column(Float, default=1)
    price = Column(Float, default=0)
    icon = Column(Text, default="")
    delivered_qty = Column(Float, nullable=True)      # сколько физически принято; NULL = ещё не скорректировано (= qty)
    status = Column(String, default="pending")       # pending | accepted | rejected
    reject_reason = Column(String, default="")
    order = relationship("Order", back_populates="items")


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), unique=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    amount = Column(Float, default=0)
    paid_amount = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    order = relationship("Order")
    user = relationship("User")


class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)
    amount = Column(Float, default=0)
    method = Column(String, default="cash")       # cash | card
    comment = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    user = relationship("User")
    invoice = relationship("Invoice")


Base.metadata.create_all(bind=engine)


def _migrate_schema():
    """Лёгкая миграция для уже существующей базы: добавляет новые колонки,
    если их ещё нет (create_all не трогает уже созданные таблицы)."""
    statements_sqlite = [
        "ALTER TABLE categories ADD COLUMN parent_id INTEGER",
        "ALTER TABLE users ADD COLUMN credit_limit FLOAT DEFAULT 0",
        "ALTER TABLE users ADD COLUMN credit_balance FLOAT DEFAULT 0",
        "ALTER TABLE products ADD COLUMN old_price FLOAT",
        "ALTER TABLE orders ADD COLUMN comment TEXT DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN payment_method VARCHAR DEFAULT ''",
        "ALTER TABLE order_items ADD COLUMN icon TEXT DEFAULT ''",
        "ALTER TABLE order_items ADD COLUMN delivered_qty FLOAT",
        "ALTER TABLE orders ADD COLUMN delivery_zone_id INTEGER",
        "ALTER TABLE orders ADD COLUMN delivery_cost FLOAT DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN promo_code VARCHAR DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN discount_amount FLOAT DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN credit_reserved FLOAT DEFAULT 0",
    ]
    statements_pg = [
        "ALTER TABLE categories ADD COLUMN IF NOT EXISTS parent_id INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_limit FLOAT DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_balance FLOAT DEFAULT 0",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS old_price FLOAT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS comment TEXT DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method VARCHAR DEFAULT ''",
        "ALTER TABLE order_items ADD COLUMN IF NOT EXISTS icon TEXT DEFAULT ''",
        "ALTER TABLE order_items ADD COLUMN IF NOT EXISTS delivered_qty FLOAT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_zone_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_cost FLOAT DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS promo_code VARCHAR DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS discount_amount FLOAT DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS credit_reserved FLOAT DEFAULT 0",
    ]
    stmts = statements_sqlite if DATABASE_URL.startswith("sqlite") else statements_pg
    with engine.connect() as conn:
        for stmt in stmts:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()  # колонка уже существует


_migrate_schema()


def _seed_catalog_if_empty():
    """При первом запуске (пустая база) наполняет каталог стартовыми товарами,
    чтобы не нужно было заходить в консоль сервера. Дальше всё редактируется
    через админ-панель в самом приложении."""
    db = SessionLocal()
    try:
        if db.query(Category).count() > 0:
            return
        seed = [
            ("Овощи и зелень", "🥕", [("Картофель", "кг", 5000), ("Помидоры", "кг", 12000), ("Зелень (укроп, петрушка)", "пучок", 3000)]),
            ("Фрукты и ягоды", "🍊", [("Яблоки", "кг", 9000), ("Бананы", "кг", 11000), ("Лимоны", "кг", 14000)]),
            ("Крупы и сухофрукты", "🌾", [("Рис", "кг", 13000), ("Гречка", "кг", 15000), ("Курага", "кг", 45000)]),
            ("Рыба и морепродукты", "🐟", [("Лосось", "кг", 120000), ("Креветки", "кг", 95000), ("Кальмар", "кг", 60000)]),
            ("Сыры и молочная продукция", "🧀", [("Молоко", "л", 9000), ("Сыр твёрдый", "кг", 85000), ("Сметана", "кг", 22000)]),
            ("Мясо и птица", "🍗", [("Курица", "кг", 32000), ("Говядина", "кг", 78000), ("Фарш", "кг", 55000)]),
            ("Масло, соусы и добавки", "🧂", [("Масло растительное", "л", 22000), ("Кетчуп", "кг", 18000), ("Майонез", "кг", 19000)]),
            ("Мука и яйца", "🥚", [("Мука", "кг", 8000), ("Яйцо", "десяток", 16000), ("Дрожжи", "100г", 6000)]),
            ("Консервы и маринады", "🥫", [("Огурцы маринованные", "банка", 14000), ("Оливки", "банка", 24000), ("Кукуруза консервир.", "банка", 12000)]),
            ("Хозтовары и прочее", "🧴", [("Салфетки", "упаковка", 8000), ("Пакеты фасовочные", "упаковка", 10000), ("Моющее средство", "л", 17000)]),
        ]
        for order, (title, icon, items) in enumerate(seed):
            cat = Category(name=title, icon=icon, sort_order=order)
            db.add(cat)
            db.flush()
            for i_order, (name, unit, price) in enumerate(items):
                db.add(Product(category_id=cat.id, name=name, unit=unit, price=price, sort_order=i_order))
        db.commit()
    finally:
        db.close()


_seed_catalog_if_empty()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ПРОВЕРКА TELEGRAM WEBAPP initData (защита от подделки доступа)
# ---------------------------------------------------------------------------
def check_telegram_auth(init_data: str) -> Optional[dict]:
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    recv_hash = pairs.pop("hash", None)
    if not recv_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if calc_hash != recv_hash:
        return None
    user_json = pairs.get("user")
    if not user_json:
        return None
    try:
        return json.loads(user_json)
    except json.JSONDecodeError:
        return None


def get_current_user(
    x_telegram_init_data: str = Header(default=""),
    init_data: str = Query(default=""),
    db: Session = Depends(get_db),
) -> User:
    tg_user = check_telegram_auth(x_telegram_init_data or init_data)
    if not tg_user:
        raise HTTPException(401, "invalid_init_data")
    tg_id = str(tg_user["id"])
    user = db.query(User).filter(User.telegram_id == tg_id).first()
    if not user:
        # Бутстрап первого админа — Telegram ID из ADMIN_TELEGRAM_IDS получает доступ автоматически
        if tg_id in ADMIN_TELEGRAM_IDS:
            user = User(
                telegram_id=tg_id,
                name=tg_user.get("first_name", "Admin"),
                role="admin",
                is_active=True,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            raise HTTPException(403, "not_registered")
    if not user.is_active:
        raise HTTPException(403, "not_registered")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admin_only")
    return user


app = FastAPI(title="Yaqin Food API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Сжимает ответы (в первую очередь /api/catalog с base64-фото) в 4-6 раз —
# без потери данных, клиент распаковывает прозрачно.
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.get("/")
def root():
    return {"ok": True, "service": "yaqin-food-backend"}


# ---------------------------------------------------------------------------
# СХЕМЫ (Pydantic)
# ---------------------------------------------------------------------------
class MeOut(BaseModel):
    id: int
    telegram_id: str
    name: str
    restaurant: str
    position: str
    role: str
    credit_limit: float = 0
    credit_balance: float = 0


class ProductOut(BaseModel):
    id: int
    name: str
    unit: str
    price: float
    old_price: Optional[float] = None
    icon: str
    in_stock: bool


class CategoryOut(BaseModel):
    id: int
    name: str
    icon: str
    parent_id: Optional[int] = None
    products: List[ProductOut] = []
    subcategories: List["CategoryOut"] = []


CategoryOut.update_forward_refs()


class PromoCodeIn(BaseModel):
    code: str
    discount_percent: Optional[float] = None
    discount_amount: Optional[float] = None
    first_order_only: bool = False
    is_active: bool = True
    max_uses: Optional[int] = None


class PromoCodeOut(BaseModel):
    id: int
    code: str
    discount_percent: Optional[float] = None
    discount_amount: Optional[float] = None
    first_order_only: bool = False
    is_active: bool = True
    max_uses: Optional[int] = None
    used_count: int = 0

    class Config:
        from_attributes = True


class PromoCheckOut(BaseModel):
    valid: bool
    reason: str = ""
    code: str = ""
    discount_percent: Optional[float] = None
    discount_amount: Optional[float] = None


class InvoiceOut(BaseModel):
    id: int
    order_id: int
    amount: float
    paid_amount: float
    remaining_amount: float
    status: str  # pending | paid
    payment_method: str = ""
    created_at: datetime
    restaurant: str = ""
    user_name: str = ""


class PaymentIn(BaseModel):
    user_id: int
    invoice_id: int
    amount: float
    method: str = "cash"
    comment: str = ""


class PaymentOut(BaseModel):
    id: int
    user_id: int
    invoice_id: Optional[int] = None
    amount: float
    method: str
    comment: str = ""
    created_at: datetime
    restaurant: str = ""
    user_name: str = ""


class OrderItemIn(BaseModel):
    name: str
    unit: str = "кг"
    qty: float
    price: float
    icon: str = ""


class OrderIn(BaseModel):
    items: List[OrderItemIn]
    comment: str = ""
    payment_method: str = ""
    delivery_time: Optional[str] = None
    promo_code: Optional[str] = None


class OrderUpdateIn(BaseModel):
    comment: Optional[str] = None
    payment_method: Optional[str] = None


class OrderItemOut(BaseModel):
    id: int
    product_name: str
    unit: str
    qty: float
    price: float
    icon: str = ""
    delivered_qty: Optional[float] = None
    status: str
    reject_reason: str


class OrderOut(BaseModel):
    id: int
    status: str
    created_at: datetime
    delivery_time: Optional[datetime] = None
    subtotal: float = 0
    discount_amount: float = 0
    promo_code: str = ""
    total: float
    items_count: int
    position: str
    restaurant: str
    user_name: str
    comment: str = ""
    payment_method: str = ""
    items: List[OrderItemOut] = []


class ItemUpdateIn(BaseModel):
    status: Optional[str] = None  # accepted | rejected
    reject_reason: str = ""
    delivered_qty: Optional[float] = None  # фактически принятое количество (может отличаться от заказанного)
    price: Optional[float] = None  # корректировка цены позиции (только админ)


class OrderStatusIn(BaseModel):
    status: str


class CategoryIn(BaseModel):
    name: str
    icon: str = ""
    sort_order: int = 0
    parent_id: Optional[int] = None


class ProductIn(BaseModel):
    category_id: int
    name: str
    unit: str = "кг"
    price: float = 0
    old_price: Optional[float] = None
    icon: str = ""
    in_stock: bool = True
    sort_order: int = 0


class UserIn(BaseModel):
    telegram_id: str
    name: str = ""
    restaurant: str = ""
    position: str = ""
    role: str = "user"
    is_active: bool = True
    credit_limit: float = 0
    credit_balance: float = 0


class UserOut(BaseModel):
    id: int
    telegram_id: str
    name: str
    restaurant: str
    position: str
    role: str
    is_active: bool
    credit_limit: float = 0
    credit_balance: float = 0

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# ХЕЛПЕРЫ
# ---------------------------------------------------------------------------
def order_to_out(o: Order) -> OrderOut:
    subtotal = sum((i.delivered_qty if i.delivered_qty is not None else i.qty) * i.price for i in o.items)
    discount = o.discount_amount or 0
    total = max(subtotal - discount, 0)
    return OrderOut(
        id=o.id,
        status=o.status,
        created_at=as_utc(o.created_at),
        # delivery_time клиент выбирает как СВОЁ локальное время (дата+слот в чекауте) и хранится
        # "как есть", без привязки к UTC — трогать не нужно, иначе сдвинется на часовой пояс.
        delivery_time=o.delivery_time,
        subtotal=subtotal,
        discount_amount=discount,
        promo_code=o.promo_code or "",
        total=total,
        items_count=len(o.items),
        position=o.user.position if o.user else "",
        restaurant=o.user.restaurant if o.user else "",
        user_name=o.user.name if o.user else "",
        comment=o.comment or "",
        payment_method=o.payment_method or "",
        items=[
            OrderItemOut(
                id=i.id, product_name=i.product_name, unit=i.unit, qty=i.qty,
                price=i.price, icon=i.icon or "", delivered_qty=i.delivered_qty,
                status=i.status, reject_reason=i.reject_reason,
            )
            for i in o.items
        ],
    )


STATUS_LABELS_RU = {
    "new": "Новый", "processing": "В обработке", "shipping": "Доставляется",
    "delivered": "Доставлен", "done": "Выполнен", "cancelled": "Отменён",
}


def _check_promo(db: Session, code: str, user: User, subtotal: float):
    """Возвращает (promo or None, discount, error_reason)."""
    if not code:
        return None, 0, ""
    promo = db.query(PromoCode).filter(PromoCode.code == code.strip().upper()).first()
    if not promo or not promo.is_active:
        return None, 0, "not_found"
    if promo.max_uses is not None and (promo.used_count or 0) >= promo.max_uses:
        return None, 0, "limit_reached"
    if promo.first_order_only:
        has_orders = db.query(Order).filter(Order.user_id == user.id).count() > 0
        if has_orders:
            return None, 0, "first_order_only"
    discount = 0.0
    if promo.discount_percent:
        discount += subtotal * (promo.discount_percent / 100)
    if promo.discount_amount:
        discount += promo.discount_amount
    discount = max(min(discount, subtotal), 0)
    return promo, discount, ""


def notify_admins_new_order(db: Session, order: Order, buyer: User):
    if not BOT_TOKEN:
        return
    admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()  # noqa: E712
    total = sum(i.qty * i.price for i in order.items)
    lines = "\n".join(f"• {i.product_name} — {i.qty} {i.unit}" for i in order.items)
    text = (
        f"🆕 Новый заказ #{order.id}\n"
        f"Заведение: {buyer.restaurant or '-'}\n"
        f"От: {buyer.name or '-'} ({buyer.position or '-'})\n\n"
        f"{lines}\n\n"
        f"Итого: {total:,.0f} сум".replace(",", " ")
    )
    for a in admins:
        payload = {"chat_id": a.telegram_id, "text": text}
        if ADMIN_WEBAPP_URL:
            payload["reply_markup"] = json.dumps({
                "inline_keyboard": [[
                    {"text": "Открыть заказ", "web_app": {"url": f"{ADMIN_WEBAPP_URL}?order={order.id}"}}
                ]]
            })
        try:
            httpx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=10)
        except Exception:
            pass  # уведомление не должно ронять создание заказа


def notify_client_order_shipping(db: Session, order: Order):
    """Уведомляем клиента в Telegram, когда его заказ переходит в статус «Доставляется»."""
    if not BOT_TOKEN:
        return
    owner = db.query(User).filter(User.id == order.user_id).first()
    if not owner or not owner.telegram_id:
        return
    text = f"🚚 Ваш заказ #{order.id} доставляется"
    payload = {"chat_id": owner.telegram_id, "text": text}
    if ADMIN_WEBAPP_URL:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": [[
                {"text": "Открыть заказ", "web_app": {"url": f"{ADMIN_WEBAPP_URL}?order={order.id}"}}
            ]]
        })
    try:
        httpx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception:
        pass  # уведомление не должно ронять смену статуса


# ---------------------------------------------------------------------------
# /api/me
# ---------------------------------------------------------------------------
@app.get("/api/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)):
    return MeOut(
        id=user.id, telegram_id=user.telegram_id, name=user.name,
        restaurant=user.restaurant, position=user.position, role=user.role,
        credit_limit=user.credit_limit or 0, credit_balance=user.credit_balance or 0,
    )


# ---------------------------------------------------------------------------
# Фото категорий/товаров: отдаём НЕ base64-строкой внутри JSON, а отдельной
# картинкой по ссылке — так браузер/Telegram кэширует её раз и навсегда,
# а /api/catalog и ответы админки остаются лёгкими и быстрыми.
# ---------------------------------------------------------------------------
def public_icon(request: Request, kind: str, obj_id: int, icon: Optional[str]) -> str:
    """Эмодзи — как есть. Фото (data:...;base64,...) — заменяем на ссылку на /api/image/..."""
    if icon and icon.startswith("data:"):
        version = hashlib.md5(icon.encode("utf-8")).hexdigest()[:10]
        base = str(request.base_url).rstrip("/")
        return f"{base}/api/image/{kind}/{obj_id}?v={version}"
    return icon or ""


@app.get("/api/image/{kind}/{obj_id}")
def get_image(kind: str, obj_id: int, db: Session = Depends(get_db)):
    if kind == "category":
        obj = db.query(Category).filter(Category.id == obj_id).first()
    elif kind == "product":
        obj = db.query(Product).filter(Product.id == obj_id).first()
    else:
        raise HTTPException(404, "not_found")
    if not obj or not obj.icon or not obj.icon.startswith("data:"):
        raise HTTPException(404, "not_found")
    try:
        header, b64data = obj.icon.split(",", 1)
        mime = header.split(";")[0][len("data:"):] or "image/jpeg"
        raw = base64.b64decode(b64data)
    except Exception:
        raise HTTPException(404, "not_found")
    return Response(
        content=raw, media_type=mime,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ---------------------------------------------------------------------------
# Каталог (просмотр — любой зарегистрированный пользователь)
# ---------------------------------------------------------------------------
@app.get("/api/catalog", response_model=List[CategoryOut])
def catalog(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cats = db.query(Category).order_by(Category.sort_order, Category.id).all()
    prods = db.query(Product).order_by(Product.sort_order, Product.id).all()

    prods_by_cat: dict = {}
    for p in prods:
        prods_by_cat.setdefault(p.category_id, []).append(p)

    cats_by_parent: dict = {}
    for c in cats:
        cats_by_parent.setdefault(c.parent_id, []).append(c)

    def build(c: Category) -> CategoryOut:
        return CategoryOut(
            id=c.id, name=c.name, icon=public_icon(request, "category", c.id, c.icon), parent_id=c.parent_id,
            products=[
                ProductOut(id=p.id, name=p.name, unit=p.unit, price=p.price, old_price=p.old_price,
                           icon=public_icon(request, "product", p.id, p.icon), in_stock=p.in_stock)
                for p in prods_by_cat.get(c.id, [])
            ],
            subcategories=[build(sc) for sc in cats_by_parent.get(c.id, [])],
        )

    return [build(c) for c in cats_by_parent.get(None, [])]


# ---------------------------------------------------------------------------
# Заказы
# ---------------------------------------------------------------------------
@app.post("/api/orders", response_model=OrderOut)
def create_order(payload: OrderIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not payload.items:
        raise HTTPException(400, "empty_order")
    delivery_dt = None
    if payload.delivery_time:
        try:
            delivery_dt = datetime.fromisoformat(payload.delivery_time.replace("Z", "+00:00"))
        except ValueError:
            delivery_dt = None
    subtotal = sum(it.qty * it.price for it in payload.items)
    promo_input = (payload.promo_code or "").strip()
    promo, discount, promo_error = _check_promo(db, promo_input, user, subtotal)
    if promo_input and not promo:
        raise HTTPException(400, f"promo_{promo_error or 'invalid'}")
    order = Order(
        user_id=user.id, status="new", comment=payload.comment or "",
        payment_method=payload.payment_method or "", delivery_time=delivery_dt,
        promo_code=promo.code if promo else "", discount_amount=discount,
    )
    db.add(order)
    db.flush()
    for it in payload.items:
        db.add(OrderItem(order_id=order.id, product_name=it.name, unit=it.unit, qty=it.qty, price=it.price, icon=it.icon or ""))
    if promo:
        promo.used_count = (promo.used_count or 0) + 1
    order_total = max(subtotal - discount, 0)
    if (payload.payment_method or "") == "credit":
        available = (user.credit_balance or 0)
        if order_total > available:
            db.rollback()
            raise HTTPException(400, "insufficient_credit")
        user.credit_balance = available - order_total
        order.credit_reserved = order_total
    db.add(Invoice(order_id=order.id, user_id=user.id, amount=order_total, paid_amount=0))
    db.commit()
    db.refresh(order)
    notify_admins_new_order(db, order, user)
    return order_to_out(order)


@app.get("/api/orders", response_model=List[OrderOut])
def list_orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(Order).order_by(Order.created_at.desc())
    if user.role != "admin":
        q = q.filter(Order.user_id == user.id)
    return [order_to_out(o) for o in q.all()]


@app.get("/api/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    o = db.query(Order).filter(Order.id == order_id).first()
    if not o or (user.role != "admin" and o.user_id != user.id):
        raise HTTPException(404, "not_found")
    return order_to_out(o)


@app.patch("/api/orders/{order_id}/items/{item_id}", response_model=OrderOut)
def update_order_item(
    order_id: int, item_id: int, payload: ItemUpdateIn,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (user.role != "admin" and order.user_id != user.id):
        raise HTTPException(404, "not_found")
    item = db.query(OrderItem).filter(OrderItem.id == item_id, OrderItem.order_id == order_id).first()
    if not item:
        raise HTTPException(404, "not_found")
    if payload.status is not None:
        was_rejected = item.status == "rejected"
        item.status = payload.status
        item.reject_reason = payload.reject_reason if payload.status == "rejected" else ""
        # позицию отклонили (и раньше она не была отклонена) — вернуть её сумму на кредитный лимит клиента
        if payload.status == "rejected" and not was_rejected and order.payment_method == "credit":
            owner = db.query(User).filter(User.id == order.user_id).first()
            if owner:
                item_amount = min((item.qty or 0) * (item.price or 0), order.credit_reserved or 0)
                order.credit_reserved = max((order.credit_reserved or 0) - item_amount, 0)
                owner.credit_balance = min((owner.credit_balance or 0) + item_amount, owner.credit_limit or 0)
    if payload.delivered_qty is not None:
        item.delivered_qty = payload.delivered_qty
    if payload.price is not None and user.role == "admin":
        item.price = payload.price
    db.commit()
    order = db.query(Order).filter(Order.id == order_id).first()
    return order_to_out(order)


@app.patch("/api/orders/{order_id}/status", response_model=OrderOut)
def update_order_status(
    order_id: int, payload: OrderStatusIn,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "not_found")
    is_owner = order.user_id == user.id
    if user.role != "admin":
        # обычный пользователь может только отменить СВОЙ заказ, и только пока он "new"
        if not is_owner or payload.status != "cancelled" or order.status != "new":
            raise HTTPException(403, "admin_only")
    was_status = order.status
    order.status = payload.status
    # заказ отменили целиком (и раньше он не был отменён) — вернуть остаток кредита клиенту
    if payload.status == "cancelled" and was_status != "cancelled" and order.payment_method == "credit" and (order.credit_reserved or 0) > 0:
        owner = db.query(User).filter(User.id == order.user_id).first()
        if owner:
            owner.credit_balance = min((owner.credit_balance or 0) + order.credit_reserved, owner.credit_limit or 0)
        order.credit_reserved = 0
    db.commit()
    # заказ перевели в "Доставляется" (и раньше он не был в этом статусе) — сообщаем клиенту в Telegram
    if payload.status == "shipping" and was_status != "shipping":
        notify_client_order_shipping(db, order)
    return order_to_out(order)


@app.patch("/api/orders/{order_id}", response_model=OrderOut)
def update_order(
    order_id: int, payload: OrderUpdateIn,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (user.role != "admin" and order.user_id != user.id):
        raise HTTPException(404, "not_found")
    if payload.comment is not None:
        order.comment = payload.comment
    if payload.payment_method is not None and payload.payment_method != order.payment_method:
        # смена способа оплаты уже после создания заказа — корректно переносим резерв кредита
        old_method, new_method = order.payment_method, payload.payment_method
        owner = db.query(User).filter(User.id == order.user_id).first()
        if old_method == "credit" and (order.credit_reserved or 0) > 0 and owner:
            owner.credit_balance = min((owner.credit_balance or 0) + order.credit_reserved, owner.credit_limit or 0)
            order.credit_reserved = 0
        if new_method == "credit":
            inv = db.query(Invoice).filter(Invoice.order_id == order.id).first()
            order_total = max((inv.amount or 0) - (inv.paid_amount or 0), 0) if inv else 0
            if owner and order_total > (owner.credit_balance or 0):
                raise HTTPException(400, "insufficient_credit")
            if owner and order_total > 0:
                owner.credit_balance = (owner.credit_balance or 0) - order_total
                order.credit_reserved = order_total
        order.payment_method = new_method
    db.commit()
    return order_to_out(order)


class OrderAddItemIn(BaseModel):
    name: str
    unit: str = "кг"
    qty: float
    price: float
    icon: str = ""


@app.post("/api/orders/{order_id}/items", response_model=OrderOut)
def add_order_item(
    order_id: int, payload: OrderAddItemIn,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or (user.role != "admin" and order.user_id != user.id):
        raise HTTPException(404, "not_found")
    db.add(OrderItem(order_id=order.id, product_name=payload.name, unit=payload.unit, qty=payload.qty, price=payload.price, icon=payload.icon or ""))
    db.commit()
    order = db.query(Order).filter(Order.id == order_id).first()
    return order_to_out(order)


@app.get("/api/promo/check", response_model=PromoCheckOut)
def check_promo(code: str, subtotal: float = 0, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    promo, discount, error = _check_promo(db, (code or "").strip(), user, subtotal)
    if not promo:
        reasons = {
            "not_found": "Промокод не найден",
            "limit_reached": "Промокод больше не действует",
            "first_order_only": "Промокод только для первого заказа",
        }
        return PromoCheckOut(valid=False, reason=reasons.get(error, "Промокод не найден"))
    return PromoCheckOut(valid=True, code=promo.code, discount_percent=promo.discount_percent, discount_amount=discount)


# ---------------------------------------------------------------------------
# Админ: каталог (категории и товары, включая фото-иконки)
# ---------------------------------------------------------------------------
@app.post("/api/admin/categories", response_model=CategoryOut)
def create_category(request: Request, payload: CategoryIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    c = Category(name=payload.name, icon=payload.icon, sort_order=payload.sort_order, parent_id=payload.parent_id)
    db.add(c)
    db.commit()
    db.refresh(c)
    return CategoryOut(id=c.id, name=c.name, icon=public_icon(request, "category", c.id, c.icon), parent_id=c.parent_id, products=[], subcategories=[])


@app.patch("/api/admin/categories/{cat_id}", response_model=CategoryOut)
def update_category(request: Request, cat_id: int, payload: CategoryIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    c = db.query(Category).filter(Category.id == cat_id).first()
    if not c:
        raise HTTPException(404, "not_found")
    c.name = payload.name
    if payload.icon:
        c.icon = payload.icon
    c.sort_order = payload.sort_order
    db.commit()
    prods = db.query(Product).filter(Product.category_id == c.id).all()
    return CategoryOut(
        id=c.id, name=c.name, icon=public_icon(request, "category", c.id, c.icon),
        products=[ProductOut(id=p.id, name=p.name, unit=p.unit, price=p.price,
                             icon=public_icon(request, "product", p.id, p.icon), in_stock=p.in_stock) for p in prods],
    )


@app.delete("/api/admin/categories/{cat_id}")
def delete_category(cat_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    child_ids = [c.id for c in db.query(Category).filter(Category.parent_id == cat_id).all()]
    all_ids = [cat_id] + child_ids
    db.query(Product).filter(Product.category_id.in_(all_ids)).delete(synchronize_session=False)
    if child_ids:
        db.query(Category).filter(Category.id.in_(child_ids)).delete(synchronize_session=False)
    db.query(Category).filter(Category.id == cat_id).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}


@app.post("/api/admin/products", response_model=ProductOut)
def create_product(request: Request, payload: ProductIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    p = Product(**payload.dict())
    db.add(p)
    db.commit()
    db.refresh(p)
    return ProductOut(id=p.id, name=p.name, unit=p.unit, price=p.price, old_price=p.old_price,
                       icon=public_icon(request, "product", p.id, p.icon), in_stock=p.in_stock)


@app.patch("/api/admin/products/{prod_id}", response_model=ProductOut)
def update_product(request: Request, prod_id: int, payload: ProductIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    p = db.query(Product).filter(Product.id == prod_id).first()
    if not p:
        raise HTTPException(404, "not_found")
    for k, v in payload.dict().items():
        if k == "icon" and not v:
            continue  # пустую иконку не затираем
        setattr(p, k, v)
    db.commit()
    return ProductOut(id=p.id, name=p.name, unit=p.unit, price=p.price, old_price=p.old_price,
                       icon=public_icon(request, "product", p.id, p.icon), in_stock=p.in_stock)


@app.delete("/api/admin/products/{prod_id}")
def delete_product(prod_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    db.query(Product).filter(Product.id == prod_id).delete()
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Админ: пользователи (кто вообще может открыть приложение)
# ---------------------------------------------------------------------------
@app.get("/api/admin/users", response_model=List[UserOut])
def list_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(User).order_by(User.created_at.desc()).all()


@app.post("/api/admin/users", response_model=UserOut)
def create_user(payload: UserIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.telegram_id == payload.telegram_id).first()
    if existing:
        raise HTTPException(400, "already_exists")
    u = User(**payload.dict())
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@app.patch("/api/admin/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "not_found")
    for k, v in payload.dict().items():
        setattr(u, k, v)
    db.commit()
    return u


@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    db.query(User).filter(User.id == user_id).delete()
    db.commit()
    return {"ok": True}


class UserCreditIn(BaseModel):
    credit_limit: Optional[float] = None
    credit_balance: Optional[float] = None


@app.patch("/api/admin/users/{user_id}/credit", response_model=UserOut)
def update_user_credit(user_id: int, payload: UserCreditIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "not_found")
    if payload.credit_limit is not None:
        u.credit_limit = payload.credit_limit
    if payload.credit_balance is not None:
        u.credit_balance = payload.credit_balance
    db.commit()
    return u


# ---------------------------------------------------------------------------
# Админ: промокоды
# ---------------------------------------------------------------------------
@app.get("/api/admin/promocodes", response_model=List[PromoCodeOut])
def list_promocodes(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(PromoCode).order_by(PromoCode.id.desc()).all()


@app.post("/api/admin/promocodes", response_model=PromoCodeOut)
def create_promocode(payload: PromoCodeIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    code = payload.code.strip().upper()
    if not code:
        raise HTTPException(400, "empty_code")
    existing = db.query(PromoCode).filter(PromoCode.code == code).first()
    if existing:
        raise HTTPException(400, "already_exists")
    p = PromoCode(
        code=code, discount_percent=payload.discount_percent, discount_amount=payload.discount_amount,
        first_order_only=payload.first_order_only, is_active=payload.is_active, max_uses=payload.max_uses,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@app.patch("/api/admin/promocodes/{promo_id}", response_model=PromoCodeOut)
def update_promocode(promo_id: int, payload: PromoCodeIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    p = db.query(PromoCode).filter(PromoCode.id == promo_id).first()
    if not p:
        raise HTTPException(404, "not_found")
    new_code = payload.code.strip().upper()
    if new_code:
        p.code = new_code
    p.discount_percent = payload.discount_percent
    p.discount_amount = payload.discount_amount
    p.first_order_only = payload.first_order_only
    p.is_active = payload.is_active
    p.max_uses = payload.max_uses
    db.commit()
    return p


@app.delete("/api/admin/promocodes/{promo_id}")
def delete_promocode(promo_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    db.query(PromoCode).filter(PromoCode.id == promo_id).delete()
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Админ: статистика
# ---------------------------------------------------------------------------
@app.get("/api/admin/stats")
def admin_stats(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.status != "cancelled").all()
    revenue = 0.0
    products_counter: Counter = Counter()
    status_counts: Counter = Counter()
    for o in orders:
        status_counts[o.status] += 1
        for i in o.items:
            if i.status == "rejected":
                continue
            qty = i.delivered_qty if i.delivered_qty is not None else i.qty
            revenue += qty * i.price
            products_counter[i.product_name] += qty
        revenue -= (o.discount_amount or 0)
    order_count = len(orders)
    avg_check = (revenue / order_count) if order_count else 0
    top_products = [{"name": n, "qty": round(q, 2)} for n, q in products_counter.most_common(10)]
    return {
        "revenue": round(revenue, 2),
        "order_count": order_count,
        "avg_check": round(avg_check, 2),
        "top_products": top_products,
        "status_counts": dict(status_counts),
    }


# ---------------------------------------------------------------------------
# Админ: экспорт заказов в Excel
# ---------------------------------------------------------------------------
@app.get("/api/admin/orders/export")
def export_orders(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    import openpyxl
    from openpyxl.styles import Font

    orders = db.query(Order).order_by(Order.created_at.desc()).all()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Заказы"
    headers = [
        "ID заказа", "Дата", "Заведение", "Клиент", "Должность", "Статус",
        "Товар", "Кол-во", "Ед.", "Цена", "Сумма", "Промокод", "Скидка", "Оплата", "Комментарий",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for o in orders:
        buyer = o.user
        status_ru = STATUS_LABELS_RU.get(o.status, o.status)
        base_row = [
            o.id, (o.created_at + timedelta(hours=5)).strftime("%d.%m.%Y %H:%M"),  # UTC -> Ташкент (UTC+5)
            buyer.restaurant if buyer else "", buyer.name if buyer else "", buyer.position if buyer else "",
            status_ru,
        ]
        if not o.items:
            ws.append(base_row + ["", "", "", "", "", o.promo_code or "", o.discount_amount or 0, o.payment_method or "", o.comment or ""])
            continue
        for i in o.items:
            qty = i.delivered_qty if i.delivered_qty is not None else i.qty
            ws.append(base_row + [
                i.product_name, qty, i.unit, i.price, qty * i.price,
                o.promo_code or "", o.discount_amount or 0, o.payment_method or "", o.comment or "",
            ])

    for col in ws.columns:
        length = max((len(str(c.value)) if c.value is not None else 0) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(length + 2, 10), 40)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"yaqin_orders_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Инвойсы (создаются автоматически на каждый заказ)
# ---------------------------------------------------------------------------
def invoice_to_out(inv: Invoice) -> InvoiceOut:
    remaining = max((inv.amount or 0) - (inv.paid_amount or 0), 0)
    status = "paid" if remaining <= 0.01 else "pending"
    return InvoiceOut(
        id=inv.id, order_id=inv.order_id, amount=inv.amount or 0, paid_amount=inv.paid_amount or 0,
        remaining_amount=remaining, status=status,
        payment_method=inv.order.payment_method if inv.order else "",
        created_at=as_utc(inv.created_at),
        restaurant=inv.user.restaurant if inv.user else "",
        user_name=inv.user.name if inv.user else "",
    )


@app.get("/api/invoices", response_model=List[InvoiceOut])
def list_invoices(status: Optional[str] = None, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(Invoice).filter(Invoice.user_id == user.id).order_by(Invoice.created_at.desc())
    invs = [invoice_to_out(i) for i in q.all()]
    if status:
        invs = [i for i in invs if i.status == status]
    return invs


@app.get("/api/admin/invoices", response_model=List[InvoiceOut])
def admin_list_invoices(
    user_id: Optional[int] = None, status: Optional[str] = None,
    admin: User = Depends(require_admin), db: Session = Depends(get_db),
):
    q = db.query(Invoice)
    if user_id:
        q = q.filter(Invoice.user_id == user_id)
    q = q.order_by(Invoice.created_at.desc())
    invs = [invoice_to_out(i) for i in q.all()]
    if status:
        invs = [i for i in invs if i.status == status]
    return invs


# ---------------------------------------------------------------------------
# Платежи (админ вручную отмечает получение оплаты по инвойсу)
# ---------------------------------------------------------------------------
def payment_to_out(p: Payment) -> PaymentOut:
    return PaymentOut(
        id=p.id, user_id=p.user_id, invoice_id=p.invoice_id, amount=p.amount or 0,
        method=p.method or "cash", comment=p.comment or "", created_at=as_utc(p.created_at),
        restaurant=p.user.restaurant if p.user else "", user_name=p.user.name if p.user else "",
    )


@app.get("/api/payments", response_model=List[PaymentOut])
def list_payments(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    q = db.query(Payment).filter(Payment.user_id == user.id).order_by(Payment.created_at.desc())
    return [payment_to_out(p) for p in q.all()]


@app.get("/api/admin/payments", response_model=List[PaymentOut])
def admin_list_payments(user_id: Optional[int] = None, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    q = db.query(Payment)
    if user_id:
        q = q.filter(Payment.user_id == user_id)
    q = q.order_by(Payment.created_at.desc())
    return [payment_to_out(p) for p in q.all()]


@app.post("/api/admin/payments", response_model=PaymentOut)
def create_payment(payload: PaymentIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    inv = db.query(Invoice).filter(Invoice.id == payload.invoice_id, Invoice.user_id == payload.user_id).first()
    if not inv:
        raise HTTPException(404, "invoice_not_found")
    if payload.amount <= 0:
        raise HTTPException(400, "invalid_amount")
    remaining = max((inv.amount or 0) - (inv.paid_amount or 0), 0)
    apply_amount = min(payload.amount, remaining) if remaining > 0 else payload.amount
    inv.paid_amount = (inv.paid_amount or 0) + apply_amount
    # оплата по заказу "на перечисление" — списанная ранее сумма кредита возвращается клиенту
    order = inv.order
    if order and order.payment_method == "credit" and (order.credit_reserved or 0) > 0:
        owner = db.query(User).filter(User.id == order.user_id).first()
        restore_amount = min(apply_amount, order.credit_reserved or 0)
        if owner and restore_amount > 0:
            owner.credit_balance = min((owner.credit_balance or 0) + restore_amount, owner.credit_limit or 0)
            order.credit_reserved = max((order.credit_reserved or 0) - restore_amount, 0)
    payment = Payment(
        user_id=payload.user_id, invoice_id=inv.id, amount=payload.amount,
        method=payload.method or "cash", comment=payload.comment or "",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment_to_out(payment)


# ---------------------------------------------------------------------------
# Мониторинг закупок (для клиента — свои данные, для админа — по любому заведению)
# ---------------------------------------------------------------------------
def _top_category_name(cat, cats_by_id):
    seen = set()
    while cat and cat.parent_id and cat.id not in seen:
        seen.add(cat.id)
        parent = cats_by_id.get(cat.parent_id)
        if not parent:
            break
        cat = parent
    return cat.name if cat else "Прочее"


def _product_category_map(db: Session) -> dict:
    cats = db.query(Category).all()
    cats_by_id = {c.id: c for c in cats}
    prods = db.query(Product).all()
    return {p.name: _top_category_name(cats_by_id.get(p.category_id), cats_by_id) for p in prods}


@app.get("/api/monitoring")
def monitoring(
    period: str = "week", user_id: Optional[int] = None,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    target_id = user.id
    if user_id and user_id != user.id:
        if user.role != "admin":
            raise HTTPException(403, "admin_only")
        target_id = user_id

    days = {"week": 7, "month": 30}.get(period)
    now = datetime.utcnow()
    period_start = now - timedelta(days=days) if days else None

    all_orders = db.query(Order).filter(Order.user_id == target_id, Order.status != "cancelled").all()
    cur_orders = [o for o in all_orders if not period_start or o.created_at >= period_start]
    prev_orders = []
    if period_start:
        prev_start = period_start - timedelta(days=days)
        prev_orders = [o for o in all_orders if prev_start <= o.created_at < period_start]

    def order_sum(o):
        s = sum((i.delivered_qty if i.delivered_qty is not None else i.qty) * i.price for i in o.items if i.status != "rejected")
        return max(s - (o.discount_amount or 0), 0)

    total_cur = sum(order_sum(o) for o in cur_orders)
    total_prev = sum(order_sum(o) for o in prev_orders)
    change_pct = round((total_cur - total_prev) / total_prev * 100, 1) if total_prev > 0 else None
    avg_purchase = round(total_cur / len(cur_orders), 2) if cur_orders else 0

    method_totals: Counter = Counter()
    for o in cur_orders:
        method_totals[o.payment_method or "cash"] += order_sum(o)
    total_methods = sum(method_totals.values()) or 1
    cash_pct = round(method_totals.get("cash", 0) / total_methods * 100, 1)
    card_pct = round(method_totals.get("card", 0) / total_methods * 100, 1)
    credit_pct = round(method_totals.get("credit", 0) / total_methods * 100, 1)

    invoices = db.query(Invoice).filter(Invoice.user_id == target_id).all()
    cur_invoices = [i for i in invoices if not period_start or i.created_at >= period_start]
    paid_total = sum(i.paid_amount or 0 for i in cur_invoices)
    remaining_total = sum(max((i.amount or 0) - (i.paid_amount or 0), 0) for i in cur_invoices)
    active_invoices = sum(1 for i in invoices if (i.amount or 0) - (i.paid_amount or 0) > 0.01)

    cat_map = _product_category_map(db)
    cat_totals: Counter = Counter()
    for o in cur_orders:
        for i in o.items:
            if i.status == "rejected":
                continue
            qty = i.delivered_qty if i.delivered_qty is not None else i.qty
            cat_totals[cat_map.get(i.product_name, "Прочее")] += qty * i.price
    cat_sum_total = sum(cat_totals.values()) or 1
    category_breakdown = [
        {"name": n, "amount": round(v, 2), "pct": round(v / cat_sum_total * 100, 1)}
        for n, v in cat_totals.most_common(8)
    ]

    daily: dict = {}
    for o in cur_orders:
        d = o.created_at.strftime("%Y-%m-%d")
        daily[d] = daily.get(d, 0) + order_sum(o)
    daily_series = [{"date": d, "amount": round(v, 2)} for d, v in sorted(daily.items())]

    target_user = db.query(User).filter(User.id == target_id).first()
    return {
        "user_id": target_id,
        "restaurant": target_user.restaurant if target_user else "",
        "total_purchases": round(total_cur, 2),
        "change_pct": change_pct,
        "avg_purchase": avg_purchase,
        "cash_pct": cash_pct,
        "card_pct": card_pct,
        "credit_pct": credit_pct,
        "paid_total": round(paid_total, 2),
        "remaining_total": round(remaining_total, 2),
        "active_invoices": active_invoices,
        "category_breakdown": category_breakdown,
        "daily_series": daily_series,
    }
