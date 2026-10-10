-- Demo database for the SQL agent project.
-- Deliberately contains every feature the agent needs to extract:
--   ENUM columns      -> the "allowed values" vocabulary
--   table/column COMMENTs -> free documentation written by a DBA
--   FOREIGN KEYs      -> how to JOIN
--   DATE / DATETIME   -> date-range reasoning
--   DECIMAL money     -> aggregation
--   a VIEW            -> a pre-built answer
--   a junk/legacy table -> so the agent must learn what to ignore

DROP DATABASE IF EXISTS shop_demo;
CREATE DATABASE shop_demo CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE shop_demo;

SET SESSION cte_max_recursion_depth = 200000;

-- ---------------------------------------------------------------- customers
CREATE TABLE customers (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  name         VARCHAR(100) NOT NULL              COMMENT 'Full name of the customer',
  email        VARCHAR(150) NOT NULL UNIQUE       COMMENT 'Login email, unique',
  city         VARCHAR(60)                        COMMENT 'Billing city',
  country      VARCHAR(60)  DEFAULT 'India'       COMMENT 'ISO country name, always India in this dataset',
  segment      ENUM('retail','sme','enterprise') NOT NULL DEFAULT 'retail'
                                                  COMMENT 'Sales segment. enterprise customers get 10% discount',
  signup_date  DATE NOT NULL                      COMMENT 'Date the account was created',
  is_active    TINYINT(1) NOT NULL DEFAULT 1      COMMENT '1 = account usable, 0 = closed or banned',
  KEY idx_customers_city (city),
  KEY idx_customers_segment (segment)
) COMMENT='One row per registered customer account';

-- --------------------------------------------------------------- categories
CREATE TABLE categories (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  name       VARCHAR(60) NOT NULL UNIQUE  COMMENT 'Display name',
  parent_id  INT NULL                     COMMENT 'Self-reference: NULL means a top-level category',
  CONSTRAINT fk_categories_parent FOREIGN KEY (parent_id) REFERENCES categories(id)
) COMMENT='Product category tree, two levels deep in this dataset';

-- ----------------------------------------------------------------- products
CREATE TABLE products (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  sku          VARCHAR(20) NOT NULL UNIQUE        COMMENT 'Stock keeping unit, format SKU-00001',
  title        VARCHAR(150) NOT NULL              COMMENT 'Product name shown to customers',
  category_id  INT NOT NULL                       COMMENT 'FK to categories',
  price        DECIMAL(10,2) NOT NULL             COMMENT 'Current list price in INR, excludes tax',
  cost         DECIMAL(10,2) NOT NULL             COMMENT 'What we pay the supplier in INR. margin = price - cost',
  status       ENUM('active','discontinued','draft') NOT NULL DEFAULT 'active'
                                                  COMMENT 'Only active products are sellable',
  created_at   DATETIME NOT NULL                  COMMENT 'When the product was added to the catalogue',
  CONSTRAINT fk_products_category FOREIGN KEY (category_id) REFERENCES categories(id),
  KEY idx_products_status (status),
  KEY idx_products_category (category_id)
) COMMENT='Product catalogue';

-- ------------------------------------------------------------------- orders
CREATE TABLE orders (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  customer_id   INT NOT NULL                      COMMENT 'FK to customers',
  order_date    DATE NOT NULL                     COMMENT 'Date the order was placed',
  status        ENUM('pending','paid','shipped','delivered','cancelled','refunded') NOT NULL DEFAULT 'pending'
                                                  COMMENT 'Lifecycle state. Revenue counts only paid/shipped/delivered',
  channel       ENUM('web','mobile','store','partner') NOT NULL
                                                  COMMENT 'Where the order came from',
  shipping_fee  DECIMAL(8,2) NOT NULL DEFAULT 0   COMMENT 'Flat shipping charge in INR',
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(id),
  KEY idx_orders_customer (customer_id),
  KEY idx_orders_date (order_date),
  KEY idx_orders_status (status)
) COMMENT='Order header. One row per checkout';

-- -------------------------------------------------------------- order_items
CREATE TABLE order_items (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  order_id    INT NOT NULL                        COMMENT 'FK to orders',
  product_id  INT NOT NULL                        COMMENT 'FK to products',
  qty         INT NOT NULL                        COMMENT 'Units bought, always >= 1',
  unit_price  DECIMAL(10,2) NOT NULL              COMMENT 'Price at time of purchase, may differ from products.price',
  CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders(id),
  CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products(id),
  KEY idx_items_order (order_id),
  KEY idx_items_product (product_id)
) COMMENT='Order lines. Line revenue = qty * unit_price';

-- ----------------------------------------------------------------- payments
CREATE TABLE payments (
  id        INT AUTO_INCREMENT PRIMARY KEY,
  order_id  INT NOT NULL                          COMMENT 'FK to orders',
  method    ENUM('card','upi','netbanking','cod') NOT NULL
                                                  COMMENT 'Payment instrument used',
  amount    DECIMAL(10,2) NOT NULL                COMMENT 'Amount captured in INR',
  status    ENUM('success','failed','pending') NOT NULL
                                                  COMMENT 'Only success means money received',
  paid_at   DATETIME NOT NULL                     COMMENT 'Timestamp of the capture attempt',
  CONSTRAINT fk_payments_order FOREIGN KEY (order_id) REFERENCES orders(id),
  KEY idx_payments_order (order_id),
  KEY idx_payments_status (status)
) COMMENT='Payment attempts. An order can have more than one row if the first attempt failed';

-- --------------------------------------------------------- legacy/junk table
CREATE TABLE tmp_import_2019 (
  row_id  INT AUTO_INCREMENT PRIMARY KEY,
  blob_txt TEXT
) COMMENT='DEPRECATED one-off import dump from 2019. Do not use in queries';

-- =============================================================== seed data

INSERT INTO categories (name, parent_id) VALUES
  ('Electronics', NULL), ('Furniture', NULL), ('Stationery', NULL);
INSERT INTO categories (name, parent_id) VALUES
  ('Keyboards', 1), ('Monitors', 1), ('Cables', 1),
  ('Desks', 2), ('Chairs', 2), ('Notebooks', 3), ('Pens', 3);

-- 2,000 customers
INSERT INTO customers (name, email, city, country, segment, signup_date, is_active)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 2000)
SELECT
  CONCAT(
    ELT(1+(n*7)%10,  'Asha','Vikram','Leela','Omar','Divya','Ravi','Nisha','Sam','Priya','Arjun'), ' ',
    ELT(1+(n*13)%8,  'Rao','Shah','Nair','Khan','Menon','Gupta','Peters','Iyer')),
  CONCAT('user', n, '@example.com'),
  ELT(1+(n*3)%8, 'Chennai','Mumbai','Kochi','Delhi','Bengaluru','Pune','Hyderabad','Kolkata'),
  'India',
  ELT(1+(n*5)%3, 'retail','sme','enterprise'),
  DATE_SUB('2026-10-01', INTERVAL (n*11)%1000 DAY),
  IF((n*17)%10 < 9, 1, 0)
FROM seq;

-- 500 products
INSERT INTO products (sku, title, category_id, price, cost, status, created_at)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 500)
SELECT
  CONCAT('SKU-', LPAD(n, 5, '0')),
  CONCAT(
    ELT(1+(n*3)%6, 'Compact','Pro','Ultra','Basic','Premium','Wireless'), ' ',
    ELT(1+(n*7)%10,'Keyboard','Monitor','Cable','Desk','Chair','Notebook','Pen','Hub','Stand','Lamp'),
    ' ', ELT(1+(n*11)%3,'Mk I','Mk II','Mk III')),
  4 + (n % 7),
  ROUND(120 + ((n*37)%19000) + 0.99, 2),
  ROUND((120 + ((n*37)%19000)) * 0.62, 2),
  ELT(1+(n*29)%10 DIV 8 + IF((n*29)%10 = 9, 1, 0), 'active','discontinued','draft'),
  DATE_SUB('2026-10-01', INTERVAL (n*13)%1200 DAY)
FROM seq;

-- 10,000 orders
INSERT INTO orders (customer_id, order_date, status, channel, shipping_fee)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 10000)
SELECT
  1 + (n*7) % 2000,
  DATE_SUB('2026-10-01', INTERVAL (n*3)%730 DAY),
  ELT(1+(n*11)%6, 'delivered','delivered','shipped','paid','pending','cancelled'),
  ELT(1+(n*5)%4,  'web','mobile','store','partner'),
  IF((n*13)%4 = 0, 0.00, ROUND(40 + (n*17)%160, 2))
FROM seq;

-- ~25,000 order lines (1-3 per order)
INSERT INTO order_items (order_id, product_id, qty, unit_price)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 3)
SELECT
  o.id,
  1 + (o.id * (7 + seq.n)) % 500,
  1 + (o.id + seq.n) % 4,
  p.price
FROM orders o
JOIN seq ON seq.n <= 1 + (o.id % 3)
JOIN products p ON p.id = 1 + (o.id * (7 + seq.n)) % 500;

-- payments for every order that got past 'pending'
INSERT INTO payments (order_id, method, amount, status, paid_at)
SELECT
  o.id,
  ELT(1+(o.id*7)%4, 'card','upi','netbanking','cod'),
  ROUND(line.total + o.shipping_fee, 2),
  IF((o.id*23)%20 = 0, 'failed', 'success'),
  TIMESTAMP(o.order_date, SEC_TO_TIME((o.id*137)%86400))
FROM orders o
JOIN (SELECT order_id, SUM(qty*unit_price) AS total FROM order_items GROUP BY order_id) line
  ON line.order_id = o.id
WHERE o.status IN ('paid','shipped','delivered','refunded');

-- retry row for the failed ones, so payments:orders is genuinely 1:many
INSERT INTO payments (order_id, method, amount, status, paid_at)
SELECT order_id, 'upi', amount, 'success', DATE_ADD(paid_at, INTERVAL 2 HOUR)
FROM payments WHERE status = 'failed';

-- ------------------------------------------------------------------ a view
CREATE VIEW v_order_revenue AS
SELECT o.id            AS order_id,
       o.customer_id,
       o.order_date,
       o.status,
       SUM(oi.qty * oi.unit_price)                  AS items_total,
       o.shipping_fee,
       SUM(oi.qty * oi.unit_price) + o.shipping_fee AS order_total
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
GROUP BY o.id, o.customer_id, o.order_date, o.status, o.shipping_fee;

ANALYZE TABLE customers, products, orders, order_items, payments;
