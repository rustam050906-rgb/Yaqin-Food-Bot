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
import hashlib
from datetime import datetime
from urllib.parse import parse_qsl
from typing import List, Optional

import httpx
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, ForeignKey, DateTime, Text, Float
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, Session
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ (задаётся переменными окружения — на Render это вкладка Environment)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./yaqin.db")
ADMIN_TELEGRAM_IDS = [x.strip() for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()]
ADMIN_WEBAPP_URL = os.environ.get("ADMIN_WEBAPP_URL", "")  # ссылка на webapp, для кнопки "Открыть заказ" в уведомлении

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # Render/Neon отдают строку вида postgres://... — SQLAlchemy 2.x хочет postgresql://
   if DATABASE_URL.startswith("postgres://"): DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1) if DATABASE_URL.startswith("postgresql://"): DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1) engine = create_engine(DATABASE_URL, pool_pre_ping=True)

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
    created_at = Column(DateTime, default=datetime.utcnow)


class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    icon = Column(Text, default="")   # emoji ИЛИ data:image/...;base64,... (фото, загруженное админом)
    sort_order = Column(Integer, default=0)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey("categories.id"))
    name = Column(String, nullable=False)
    unit = Column(String, default="кг")
    price = Column(Float, default=0)
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
    user = relationship("User")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    product_name = Column(String)
    unit = Column(String, default="кг")
    qty = Column(Float, default=1)
    price = Column(Float, default=0)
    status = Column(String, default="pending")       # pending | accepted | rejected
    reject_reason = Column(String, default="")
    order = relationship("Order", back_populates="items")


Base.metadata.create_all(bind=engine)


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
    db: Session = Depends(get_db),
) -> User:
    tg_user = check_telegram_auth(x_telegram_init_data)
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


class ProductOut(BaseModel):
    id: int
    name: str
    unit: str
    price: float
    icon: str
    in_stock: bool


class CategoryOut(BaseModel):
    id: int
    name: str
    icon: str
    products: List[ProductOut] = []


class OrderItemIn(BaseModel):
    name: str
    unit: str = "кг"
    qty: float
    price: float


class OrderIn(BaseModel):
    items: List[OrderItemIn]


class OrderItemOut(BaseModel):
    id: int
    product_name: str
    unit: str
    qty: float
    price: float
    status: str
    reject_reason: str


class OrderOut(BaseModel):
    id: int
    status: str
    created_at: datetime
    delivery_time: Optional[datetime] = None
    total: float
    items_count: int
    position: str
    restaurant: str
    user_name: str
    items: List[OrderItemOut] = []


class ItemUpdateIn(BaseModel):
    status: str  # accepted | rejected
    reject_reason: str = ""


class OrderStatusIn(BaseModel):
    status: str


class CategoryIn(BaseModel):
    name: str
    icon: str = ""
    sort_order: int = 0


class ProductIn(BaseModel):
    category_id: int
    name: str
    unit: str = "кг"
    price: float = 0
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


class UserOut(BaseModel):
    id: int
    telegram_id: str
    name: str
    restaurant: str
    position: str
    role: str
    is_active: bool

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# ХЕЛПЕРЫ
# ---------------------------------------------------------------------------
def order_to_out(o: Order) -> OrderOut:
    total = sum(i.qty * i.price for i in o.items)
    return OrderOut(
        id=o.id,
        status=o.status,
        created_at=o.created_at,
        delivery_time=o.delivery_time,
        total=total,
        items_count=len(o.items),
        position=o.user.position if o.user else "",
        restaurant=o.user.restaurant if o.user else "",
        user_name=o.user.name if o.user else "",
        items=[
            OrderItemOut(
                id=i.id, product_name=i.product_name, unit=i.unit, qty=i.qty,
                price=i.price, status=i.status, reject_reason=i.reject_reason,
            )
            for i in o.items
        ],
    )


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


# ---------------------------------------------------------------------------
# /api/me
# ---------------------------------------------------------------------------
@app.get("/api/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)):
    return MeOut(
        id=user.id, telegram_id=user.telegram_id, name=user.name,
        restaurant=user.restaurant, position=user.position, role=user.role,
    )


# ---------------------------------------------------------------------------
# Каталог (просмотр — любой зарегистрированный пользователь)
# ---------------------------------------------------------------------------
@app.get("/api/catalog", response_model=List[CategoryOut])
def catalog(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cats = db.query(Category).order_by(Category.sort_order, Category.id).all()
    out = []
    for c in cats:
        prods = (
            db.query(Product)
            .filter(Product.category_id == c.id)
            .order_by(Product.sort_order, Product.id)
            .all()
        )
        out.append(CategoryOut(
            id=c.id, name=c.name, icon=c.icon or "",
            products=[
                ProductOut(id=p.id, name=p.name, unit=p.unit, price=p.price, icon=p.icon or "", in_stock=p.in_stock)
                for p in prods
            ],
        ))
    return out


# ---------------------------------------------------------------------------
# Заказы
# ---------------------------------------------------------------------------
@app.post("/api/orders", response_model=OrderOut)
def create_order(payload: OrderIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not payload.items:
        raise HTTPException(400, "empty_order")
    order = Order(user_id=user.id, status="new")
    db.add(order)
    db.flush()
    for it in payload.items:
        db.add(OrderItem(order_id=order.id, product_name=it.name, unit=it.unit, qty=it.qty, price=it.price))
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
    admin: User = Depends(require_admin), db: Session = Depends(get_db),
):
    item = db.query(OrderItem).filter(OrderItem.id == item_id, OrderItem.order_id == order_id).first()
    if not item:
        raise HTTPException(404, "not_found")
    item.status = payload.status
    item.reject_reason = payload.reject_reason if payload.status == "rejected" else ""
    db.commit()
    order = db.query(Order).filter(Order.id == order_id).first()
    return order_to_out(order)


@app.patch("/api/orders/{order_id}/status", response_model=OrderOut)
def update_order_status(
    order_id: int, payload: OrderStatusIn,
    admin: User = Depends(require_admin), db: Session = Depends(get_db),
):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "not_found")
    order.status = payload.status
    db.commit()
    return order_to_out(order)


# ---------------------------------------------------------------------------
# Админ: каталог (категории и товары, включая фото-иконки)
# ---------------------------------------------------------------------------
@app.post("/api/admin/categories", response_model=CategoryOut)
def create_category(payload: CategoryIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    c = Category(name=payload.name, icon=payload.icon, sort_order=payload.sort_order)
    db.add(c)
    db.commit()
    db.refresh(c)
    return CategoryOut(id=c.id, name=c.name, icon=c.icon, products=[])


@app.patch("/api/admin/categories/{cat_id}", response_model=CategoryOut)
def update_category(cat_id: int, payload: CategoryIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
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
        id=c.id, name=c.name, icon=c.icon,
        products=[ProductOut(id=p.id, name=p.name, unit=p.unit, price=p.price, icon=p.icon, in_stock=p.in_stock) for p in prods],
    )


@app.delete("/api/admin/categories/{cat_id}")
def delete_category(cat_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    db.query(Product).filter(Product.category_id == cat_id).delete()
    db.query(Category).filter(Category.id == cat_id).delete()
    db.commit()
    return {"ok": True}


@app.post("/api/admin/products", response_model=ProductOut)
def create_product(payload: ProductIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    p = Product(**payload.dict())
    db.add(p)
    db.commit()
    db.refresh(p)
    return ProductOut(id=p.id, name=p.name, unit=p.unit, price=p.price, icon=p.icon, in_stock=p.in_stock)


@app.patch("/api/admin/products/{prod_id}", response_model=ProductOut)
def update_product(prod_id: int, payload: ProductIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    p = db.query(Product).filter(Product.id == prod_id).first()
    if not p:
        raise HTTPException(404, "not_found")
    for k, v in payload.dict().items():
        if k == "icon" and not v:
            continue  # пустую иконку не затираем
        setattr(p, k, v)
    db.commit()
    return ProductOut(id=p.id, name=p.name, unit=p.unit, price=p.price, icon=p.icon, in_stock=p.in_stock)


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
