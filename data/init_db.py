"""生成示例数据库 data/shop.db：一个小型电商（商品、客户、订单、评价）。

用法：python -m data.init_db
数据用固定随机种子生成，每次运行结果都一样，这样评估集（阶段 5）的标准答案才稳定。
"""

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from common.config import DEFAULT_DB

SCHEMA = """
CREATE TABLE categories (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL              -- 类目名称
);
CREATE TABLE products (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,      -- 商品名称
    category_id INTEGER NOT NULL REFERENCES categories(id),
    price       REAL NOT NULL,      -- 当前售价（元）
    stock       INTEGER NOT NULL    -- 库存数量
);
CREATE TABLE customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,      -- 客户姓名
    city        TEXT NOT NULL,      -- 所在城市
    phone       TEXT NOT NULL,      -- 手机号（敏感字段，阶段 5 会做脱敏）
    signup_date TEXT NOT NULL       -- 注册日期 YYYY-MM-DD
);
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date  TEXT NOT NULL,      -- 下单日期 YYYY-MM-DD
    status      TEXT NOT NULL       -- 已完成 / 已取消 / 已退款
);
CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity   INTEGER NOT NULL,    -- 购买数量
    unit_price REAL NOT NULL        -- 成交单价（元）
);
CREATE TABLE reviews (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    rating      INTEGER NOT NULL,   -- 1~5 星
    content     TEXT NOT NULL,      -- 评价内容（用户输入的文本！阶段 5 用它演示提示注入）
    created_at  TEXT NOT NULL
);
"""

CATEGORIES = ["手机数码", "家用电器", "图书", "食品饮料", "运动户外"]
PRODUCTS = [
    # (名称, 类目序号, 价格)
    ("星云 X5 手机", 0, 2999), ("星云 X5 Pro 手机", 0, 3999), ("轻听 无线耳机", 0, 399),
    ("闪充 充电宝 20000mAh", 0, 149), ("极光 平板电脑", 0, 2499), ("智环 运动手环", 0, 229),
    ("净风 空气净化器", 1, 1299), ("暖居 电暖器", 1, 459), ("清泉 净水器", 1, 1899),
    ("速烹 空气炸锅", 1, 399), ("静眠 加湿器", 1, 199), ("洁净 扫地机器人", 1, 1699),
    ("《Python 编程入门》", 2, 69), ("《深入理解计算机系统》", 2, 139), ("《大模型应用开发》", 2, 89),
    ("《三体》全集", 2, 99), ("《算法导论》", 2, 128), ("《活着》", 2, 39),
    ("山野 坚果礼盒", 3, 128), ("醇香 挂耳咖啡 50 包", 3, 89), ("清茶 龙井 250g", 3, 168),
    ("鲜榨 橙汁 12 瓶", 3, 59), ("能量 燕麦片 1kg", 3, 45), ("零糖 苏打水 24 瓶", 3, 52),
    ("疾风 跑步鞋", 4, 599), ("登峰 双肩包", 4, 329), ("瑜伽垫 加厚款", 4, 99),
    ("骑行 头盔", 4, 259), ("露营 帐篷 双人", 4, 699), ("速干 运动T恤", 4, 79),
]
CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京", "西安", "重庆"]
SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗高"
GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "洋", "艳", "勇", "军", "杰", "娟", "涛", "明", "超", "霞", "平", "刚", "桂英", "子涵"]
REVIEW_TEXT = {
    5: ["非常满意，物流很快", "质量超出预期，推荐", "第二次购买了，一如既往的好"],
    4: ["整体不错，包装可以再好点", "性价比高", "用着挺好，就是颜色和图片有点差别"],
    3: ["一般般吧", "还行，没有特别惊艳", "中规中矩"],
    2: ["有点失望，做工粗糙", "和描述不太一样"],
    1: ["质量很差，已申请退货", "用了两天就坏了"],
}

START = date(2026, 1, 1)
END = date(2026, 9, 30)


def build(db_path: Path) -> None:
    rng = random.Random(42)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)

    conn.executemany("INSERT INTO categories VALUES (?, ?)", [(i + 1, n) for i, n in enumerate(CATEGORIES)])
    conn.executemany(
        "INSERT INTO products VALUES (?, ?, ?, ?, ?)",
        [(i + 1, n, c + 1, p, rng.randint(0, 300)) for i, (n, c, p) in enumerate(PRODUCTS)],
    )

    customers = []
    for i in range(1, 81):
        name = rng.choice(SURNAMES) + rng.choice(GIVEN)
        phone = "1" + rng.choice("3589") + "".join(rng.choice("0123456789") for _ in range(9))
        signup = START - timedelta(days=rng.randint(0, 400))
        customers.append((i, name, rng.choice(CITIES), phone, signup.isoformat()))
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers)

    # 热门商品被买的概率更高，让"销量排行"这类问题有明显的答案
    weights = [rng.uniform(0.3, 3.0) for _ in PRODUCTS]
    order_id = item_id = 0
    days = (END - START).days
    for _ in range(700):
        order_id += 1
        order_date = START + timedelta(days=rng.randint(0, days))
        status = rng.choices(["已完成", "已取消", "已退款"], weights=[85, 10, 5])[0]
        conn.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?)",
            (order_id, rng.randint(1, len(customers)), order_date.isoformat(), status),
        )
        for pid in set(rng.choices(range(1, len(PRODUCTS) + 1), weights=weights, k=rng.randint(1, 3))):
            item_id += 1
            price = PRODUCTS[pid - 1][2]
            # 有时打折成交
            unit_price = round(price * rng.choice([1, 1, 1, 0.9, 0.8]), 2)
            conn.execute(
                "INSERT INTO order_items VALUES (?, ?, ?, ?, ?)",
                (item_id, order_id, pid, rng.randint(1, 3), unit_price),
            )

    for rid in range(1, 201):
        rating = rng.choices([5, 4, 3, 2, 1], weights=[40, 30, 15, 10, 5])[0]
        created = START + timedelta(days=rng.randint(0, days))
        conn.execute(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
            (rid, rng.randint(1, len(PRODUCTS)), rng.randint(1, len(customers)), rating,
             rng.choice(REVIEW_TEXT[rating]), created.isoformat()),
        )

    conn.commit()
    conn.close()


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    build(path)
    conn = sqlite3.connect(path)
    for table in ["categories", "products", "customers", "orders", "order_items", "reviews"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:12s} {count:5d} 行")
    print(f"已生成数据库：{path}")


if __name__ == "__main__":
    main()
