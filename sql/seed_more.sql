SET SESSION cte_max_recursion_depth = 200000;
-- Three more test databases, each stressing something different.
--
--   hr_demo       NO comments anywhere, self-referencing FK, NULL-heavy columns
--   library_demo  many-to-many join table, NULL means "still out" (not "zero")
--   saas_demo     15 tables - tests whether the overview-first design holds up
--
-- Run with:  mysql -u root -p < sql/seed_more.sql

-- =====================================================================
-- hr_demo : the "undocumented database" case. No COMMENT anywhere, so the
--           agent has only names, types, and the profiled values to go on.
-- =====================================================================
DROP DATABASE IF EXISTS hr_demo;
CREATE DATABASE hr_demo CHARACTER SET utf8mb4;
USE hr_demo;

CREATE TABLE departments (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(60) NOT NULL UNIQUE,
  cost_center VARCHAR(12)
);

CREATE TABLE employees (
  id               INT AUTO_INCREMENT PRIMARY KEY,
  full_name        VARCHAR(100) NOT NULL,
  dept_id          INT,
  manager_id       INT,                       -- self FK, NULL for the top of the tree
  salary           DECIMAL(12,2),
  hire_date        DATE NOT NULL,
  termination_date DATE,                      -- NULL = still employed
  emp_status       ENUM('active','on_leave','terminated') NOT NULL DEFAULT 'active',
  CONSTRAINT fk_emp_dept FOREIGN KEY (dept_id) REFERENCES departments(id),
  CONSTRAINT fk_emp_mgr  FOREIGN KEY (manager_id) REFERENCES employees(id),
  KEY idx_emp_dept (dept_id),
  KEY idx_emp_status (emp_status)
);

CREATE TABLE leave_requests (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  employee_id INT NOT NULL,
  leave_type  ENUM('casual','sick','unpaid','maternity') NOT NULL,
  start_date  DATE NOT NULL,
  end_date    DATE NOT NULL,
  approved    TINYINT(1),                     -- NULL = not decided yet
  CONSTRAINT fk_leave_emp FOREIGN KEY (employee_id) REFERENCES employees(id),
  KEY idx_leave_emp (employee_id)
);

INSERT INTO departments (name, cost_center) VALUES
  ('Engineering','CC-100'), ('Sales','CC-200'), ('Support','CC-300'),
  ('Finance','CC-400'), ('People', NULL);

-- two leaders with no manager, then 118 reports
INSERT INTO employees (full_name, dept_id, manager_id, salary, hire_date, termination_date, emp_status)
VALUES ('Meera Krishnan', 1, NULL, 6800000, '2019-02-01', NULL, 'active'),
       ('Daniel Fernandes', 2, NULL, 6200000, '2019-05-15', NULL, 'active');

INSERT INTO employees (full_name, dept_id, manager_id, salary, hire_date, termination_date, emp_status)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 118)
SELECT
  CONCAT(ELT(1+(n*7)%10,'Rohit','Kavya','Imran','Swathi','Anand','Fatima','Girish','Tara','Manoj','Ishita'),
         ' ',
         ELT(1+(n*11)%8,'Kumar','Reddy','Pillai','Bose','Dutta','Joshi','Varma','Sethi')),
  1 + (n*3) % 5,
  1 + (n % 2),
  ROUND(600000 + ((n*8237) % 2600000), -3),
  DATE_SUB('2026-10-01', INTERVAL (n*23)%2400 DAY),
  -- ~12% have left; the rest are NULL
  IF((n*31)%8 = 0, DATE_SUB('2026-10-01', INTERVAL (n*7)%400 DAY), NULL),
  CASE WHEN (n*31)%8 = 0 THEN 'terminated'
       WHEN (n*17)%11 = 0 THEN 'on_leave'
       ELSE 'active' END
FROM seq;

INSERT INTO leave_requests (employee_id, leave_type, start_date, end_date, approved)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 300)
SELECT
  1 + (n*13) % 120,
  ELT(1+(n*5)%4, 'casual','sick','unpaid','maternity'),
  DATE_SUB('2026-10-01', INTERVAL (n*3)%700 DAY),
  DATE_SUB('2026-10-01', INTERVAL ((n*3)%700) - (1+(n%5)) DAY),
  CASE (n*7)%10 WHEN 0 THEN NULL WHEN 1 THEN NULL WHEN 2 THEN 0 ELSE 1 END
FROM seq;

ANALYZE TABLE departments, employees, leave_requests;

-- =====================================================================
-- library_demo : NULL carries meaning. returned_on IS NULL means the book is
--                still out - a count of loans is NOT a count of books borrowed
--                and never returned.
-- =====================================================================
DROP DATABASE IF EXISTS library_demo;
CREATE DATABASE library_demo CHARACTER SET utf8mb4;
USE library_demo;

CREATE TABLE authors (
  id      INT AUTO_INCREMENT PRIMARY KEY,
  name    VARCHAR(100) NOT NULL COMMENT 'Author display name',
  country VARCHAR(60)           COMMENT 'Country of origin, NULL when unknown'
) COMMENT='Book authors';

CREATE TABLE books (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  isbn           VARCHAR(20) NOT NULL UNIQUE COMMENT 'ISBN-13',
  title          VARCHAR(200) NOT NULL,
  published_year SMALLINT              COMMENT 'Year of first publication',
  genre          ENUM('fiction','nonfiction','reference','childrens','poetry') NOT NULL,
  copies_total   INT NOT NULL DEFAULT 1 COMMENT 'How many physical copies the library owns'
) COMMENT='Catalogue. One row per title, not per physical copy';

CREATE TABLE book_authors (
  book_id   INT NOT NULL,
  author_id INT NOT NULL,
  PRIMARY KEY (book_id, author_id),
  CONSTRAINT fk_ba_book   FOREIGN KEY (book_id)   REFERENCES books(id),
  CONSTRAINT fk_ba_author FOREIGN KEY (author_id) REFERENCES authors(id)
) COMMENT='Many-to-many: a book can have several authors and vice versa';

CREATE TABLE members (
  id         INT AUTO_INCREMENT PRIMARY KEY,
  name       VARCHAR(100) NOT NULL,
  join_date  DATE NOT NULL,
  membership ENUM('basic','premium','student') NOT NULL DEFAULT 'basic'
                         COMMENT 'premium may borrow 10 books, basic 3, student 5',
  is_blocked TINYINT(1) NOT NULL DEFAULT 0
                         COMMENT '1 = cannot borrow, usually unpaid fines'
) COMMENT='Library members';

CREATE TABLE loans (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  book_id     INT NOT NULL,
  member_id   INT NOT NULL,
  loaned_on   DATE NOT NULL,
  due_on      DATE NOT NULL  COMMENT 'loaned_on + 14 days',
  returned_on DATE           COMMENT 'NULL means the book is STILL OUT. Overdue = returned_on IS NULL AND due_on < today',
  fine_amount DECIMAL(8,2) NOT NULL DEFAULT 0 COMMENT 'Fine charged in INR, 0 when none',
  CONSTRAINT fk_loan_book   FOREIGN KEY (book_id)   REFERENCES books(id),
  CONSTRAINT fk_loan_member FOREIGN KEY (member_id) REFERENCES members(id),
  KEY idx_loan_member (member_id),
  KEY idx_loan_returned (returned_on)
) COMMENT='Borrowing history';

INSERT INTO authors (name, country)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 60)
SELECT CONCAT(ELT(1+(n*3)%10,'R. K.','Anita','Jhumpa','Salman','Arundhati','Vikram','Amitav','Kiran','Rohinton','Shashi'),
              ' ',
              ELT(1+(n*7)%6,'Narayan','Desai','Lahiri','Rushdie','Roy','Seth')),
       IF((n*5)%7 = 0, NULL, ELT(1+(n*11)%4,'India','United Kingdom','United States','Canada'))
FROM seq;

INSERT INTO books (isbn, title, published_year, genre, copies_total)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 400)
SELECT CONCAT('978', LPAD(n*7919 % 10000000000, 10, '0')),
       CONCAT(ELT(1+(n*3)%8,'The Last','A Fine','Midnight','The White','Interpreter of','The God of','Sea of','The Great'),
              ' ',
              ELT(1+(n*5)%8,'Balance','Children','Tiger','Maladies','Small Things','Poppies','Guide','Monsoon'),
              IF((n%4)=0, CONCAT(' Vol ', 1+(n%3)), '')),
       IF((n*13)%17 = 0, NULL, 1950 + (n*7)%76),
       ELT(1+(n*11)%5, 'fiction','nonfiction','reference','childrens','poetry'),
       1 + (n*3)%5
FROM seq;

INSERT INTO book_authors (book_id, author_id)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 2)
SELECT b.id, 1 + (b.id * (3 + seq.n)) % 60
FROM books b JOIN seq ON seq.n <= 1 + (b.id % 2)
GROUP BY b.id, 1 + (b.id * (3 + seq.n)) % 60;

INSERT INTO members (name, join_date, membership, is_blocked)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 300)
SELECT CONCAT(ELT(1+(n*7)%10,'Deepa','Karthik','Sneha','Arjun','Lakshmi','Varun','Pooja','Nikhil','Shreya','Aditya'),
              ' ', ELT(1+(n*13)%6,'Menon','Raj','Chopra','Bhatt','Naidu','Saxena')),
       DATE_SUB('2026-10-01', INTERVAL (n*11)%1800 DAY),
       ELT(1+(n*3)%3, 'basic','premium','student'),
       IF((n*19)%15 = 0, 1, 0)
FROM seq;

-- 3,000 loans. ~18% never returned; of those, most are already overdue.
INSERT INTO loans (book_id, member_id, loaned_on, due_on, returned_on, fine_amount)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 3000)
SELECT
  1 + (n*7) % 400,
  1 + (n*13) % 300,
  DATE_SUB('2026-10-01', INTERVAL (n*5)%900 DAY) AS loaned_on,
  DATE_ADD(DATE_SUB('2026-10-01', INTERVAL (n*5)%900 DAY), INTERVAL 14 DAY) AS due_on,
  -- ~18% never returned; and a return date in the future means still out, so NULL
  CASE WHEN (n*7)%11 >= 9 THEN NULL
       WHEN DATE_ADD(DATE_SUB('2026-10-01', INTERVAL (n*5)%900 DAY),
                     INTERVAL (5 + (n*3)%25) DAY) > '2026-10-01' THEN NULL
       ELSE DATE_ADD(DATE_SUB('2026-10-01', INTERVAL (n*5)%900 DAY),
                     INTERVAL (5 + (n*3)%25) DAY)
  END,
  CASE WHEN (n*7)%11 >= 9 THEN 0
       WHEN (n*3)%25 > 9 THEN ROUND(5 * ((n*3)%25 - 9), 2)
       ELSE 0 END
FROM seq;

ANALYZE TABLE authors, books, book_authors, members, loans;

-- =====================================================================
-- saas_demo : 15 tables, thin data. The point is schema BREADTH - proves the
--             agent navigates by list_tables + describe_table instead of
--             needing the whole schema in the prompt.
-- =====================================================================
DROP DATABASE IF EXISTS saas_demo;
CREATE DATABASE saas_demo CHARACTER SET utf8mb4;
USE saas_demo;

CREATE TABLE accounts (
  id INT AUTO_INCREMENT PRIMARY KEY,
  company_name VARCHAR(120) NOT NULL,
  created_on DATE NOT NULL,
  account_status ENUM('trial','active','churned','suspended') NOT NULL DEFAULT 'trial',
  region ENUM('apac','emea','namer','latam') NOT NULL
) COMMENT='Customer organisations';

CREATE TABLE users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  account_id INT NOT NULL,
  email VARCHAR(150) NOT NULL UNIQUE,
  full_name VARCHAR(100) NOT NULL,
  user_role ENUM('owner','admin','member','viewer') NOT NULL DEFAULT 'member',
  last_login_at DATETIME COMMENT 'NULL = never logged in',
  CONSTRAINT fk_users_account FOREIGN KEY (account_id) REFERENCES accounts(id)
) COMMENT='Individual logins, belong to one account';

CREATE TABLE teams (
  id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, name VARCHAR(80) NOT NULL,
  CONSTRAINT fk_teams_account FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE TABLE team_members (
  team_id INT NOT NULL, user_id INT NOT NULL, PRIMARY KEY (team_id, user_id),
  CONSTRAINT fk_tm_team FOREIGN KEY (team_id) REFERENCES teams(id),
  CONSTRAINT fk_tm_user FOREIGN KEY (user_id) REFERENCES users(id)
) COMMENT='Many-to-many between teams and users';

CREATE TABLE plans (
  id INT AUTO_INCREMENT PRIMARY KEY, code VARCHAR(20) NOT NULL UNIQUE,
  name VARCHAR(60) NOT NULL, monthly_price DECIMAL(10,2) NOT NULL,
  seat_limit INT COMMENT 'NULL = unlimited seats'
) COMMENT='Price book';

CREATE TABLE subscriptions (
  id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, plan_id INT NOT NULL,
  started_on DATE NOT NULL, ended_on DATE COMMENT 'NULL = currently active',
  seats INT NOT NULL DEFAULT 1,
  CONSTRAINT fk_sub_account FOREIGN KEY (account_id) REFERENCES accounts(id),
  CONSTRAINT fk_sub_plan FOREIGN KEY (plan_id) REFERENCES plans(id)
) COMMENT='One row per plan period. MRR comes from here, not from invoices';

CREATE TABLE invoices (
  id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, issued_on DATE NOT NULL,
  due_on DATE NOT NULL, invoice_status ENUM('draft','sent','paid','void','overdue') NOT NULL,
  total DECIMAL(12,2) NOT NULL,
  CONSTRAINT fk_inv_account FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE TABLE invoice_lines (
  id INT AUTO_INCREMENT PRIMARY KEY, invoice_id INT NOT NULL, description VARCHAR(150) NOT NULL,
  quantity INT NOT NULL, unit_amount DECIMAL(10,2) NOT NULL,
  CONSTRAINT fk_il_invoice FOREIGN KEY (invoice_id) REFERENCES invoices(id)
);

CREATE TABLE payments (
  id INT AUTO_INCREMENT PRIMARY KEY, invoice_id INT NOT NULL, paid_on DATE NOT NULL,
  amount DECIMAL(12,2) NOT NULL, method ENUM('card','ach','wire','credit') NOT NULL,
  CONSTRAINT fk_pay_invoice FOREIGN KEY (invoice_id) REFERENCES invoices(id)
);

CREATE TABLE features (
  id INT AUTO_INCREMENT PRIMARY KEY, code VARCHAR(40) NOT NULL UNIQUE, name VARCHAR(80) NOT NULL
);

CREATE TABLE plan_features (
  plan_id INT NOT NULL, feature_id INT NOT NULL, PRIMARY KEY (plan_id, feature_id),
  CONSTRAINT fk_pf_plan FOREIGN KEY (plan_id) REFERENCES plans(id),
  CONSTRAINT fk_pf_feature FOREIGN KEY (feature_id) REFERENCES features(id)
) COMMENT='Which features each plan unlocks';

CREATE TABLE feature_usage (
  id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, feature_id INT NOT NULL,
  usage_date DATE NOT NULL, event_count INT NOT NULL,
  CONSTRAINT fk_fu_account FOREIGN KEY (account_id) REFERENCES accounts(id),
  CONSTRAINT fk_fu_feature FOREIGN KEY (feature_id) REFERENCES features(id),
  KEY idx_fu_date (usage_date)
) COMMENT='Daily rollup of feature events per account';

CREATE TABLE tickets (
  id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, opened_by INT NOT NULL,
  opened_at DATETIME NOT NULL, closed_at DATETIME COMMENT 'NULL = still open',
  priority ENUM('low','normal','high','urgent') NOT NULL DEFAULT 'normal',
  ticket_status ENUM('open','pending','solved','closed') NOT NULL DEFAULT 'open',
  subject VARCHAR(200) NOT NULL,
  CONSTRAINT fk_tk_account FOREIGN KEY (account_id) REFERENCES accounts(id),
  CONSTRAINT fk_tk_user FOREIGN KEY (opened_by) REFERENCES users(id)
) COMMENT='Support tickets';

CREATE TABLE ticket_messages (
  id INT AUTO_INCREMENT PRIMARY KEY, ticket_id INT NOT NULL, sent_at DATETIME NOT NULL,
  from_support TINYINT(1) NOT NULL COMMENT '1 = written by our team, 0 = by the customer',
  body TEXT NOT NULL,
  CONSTRAINT fk_tmsg_ticket FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);

CREATE TABLE audit_log (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, account_id INT, user_id INT,
  occurred_at DATETIME NOT NULL, action VARCHAR(60) NOT NULL, detail VARCHAR(255),
  KEY idx_audit_time (occurred_at)
) COMMENT='Append-only. High volume, avoid unfiltered scans';

-- ---- data
INSERT INTO plans (code, name, monthly_price, seat_limit) VALUES
  ('FREE','Free',0,3), ('START','Starter',2900,10), ('GROW','Growth',9900,50),
  ('SCALE','Scale',29900,200), ('ENT','Enterprise',99900,NULL);

INSERT INTO features (code, name) VALUES
  ('sso','Single sign-on'), ('api','API access'), ('audit','Audit log'),
  ('webhook','Webhooks'), ('sandbox','Sandbox environment'), ('priority','Priority support');

INSERT INTO plan_features (plan_id, feature_id) VALUES
  (2,2),(3,2),(3,4),(4,1),(4,2),(4,3),(4,4),(5,1),(5,2),(5,3),(5,4),(5,5),(5,6);

INSERT INTO accounts (company_name, created_on, account_status, region)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 200)
SELECT CONCAT(ELT(1+(n*3)%10,'Northwind','Acme','Globex','Initech','Umbrella','Stark','Wayne','Soylent','Tyrell','Hooli'),
              ' ', ELT(1+(n*7)%5,'Labs','Systems','Digital','Partners','Industries')),
       DATE_SUB('2026-10-01', INTERVAL (n*9)%1500 DAY),
       ELT(1+(n*11)%4, 'active','active','trial','churned'),
       ELT(1+(n*5)%4, 'apac','emea','namer','latam')
FROM seq;

INSERT INTO users (account_id, email, full_name, user_role, last_login_at)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 900)
SELECT 1 + (n*7)%200, CONCAT('u', n, '@example.com'),
       CONCAT(ELT(1+(n*3)%8,'Alex','Bea','Chen','Dana','Elif','Femi','Gita','Hugo'), ' ',
              ELT(1+(n*5)%6,'Park','Silva','Okafor','Novak','Haddad','Lund')),
       ELT(1+(n*13)%4, 'member','member','admin','owner'),
       IF((n*17)%9 = 0, NULL, DATE_SUB('2026-10-01', INTERVAL (n*3)%200 DAY))
FROM seq;

INSERT INTO teams (account_id, name)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 260)
SELECT 1 + (n*3)%200, ELT(1+(n*7)%5,'Platform','Growth','Data','Design','Ops')
FROM seq;

INSERT INTO team_members (team_id, user_id)
SELECT t.id, u.id FROM teams t JOIN users u
  ON u.account_id = t.account_id AND (u.id + t.id) % 3 = 0;

INSERT INTO subscriptions (account_id, plan_id, started_on, ended_on, seats)
SELECT a.id, 1 + (a.id*3)%5, a.created_on,
       IF(a.account_status = 'churned', DATE_ADD(a.created_on, INTERVAL 200 DAY), NULL),
       1 + (a.id*7)%30
FROM accounts a;

INSERT INTO invoices (account_id, issued_on, due_on, invoice_status, total)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 1200)
SELECT 1 + (n*7)%200,
       DATE_SUB('2026-10-01', INTERVAL (n*3)%600 DAY),
       DATE_SUB('2026-10-01', INTERVAL ((n*3)%600) - 30 DAY),
       ELT(1+(n*11)%5, 'paid','paid','paid','sent','overdue'),
       ROUND(2900 + (n*811)%97000, 2)
FROM seq;

INSERT INTO invoice_lines (invoice_id, description, quantity, unit_amount)
SELECT i.id, CONCAT('Subscription - ', ELT(1+(i.id%5),'Free','Starter','Growth','Scale','Enterprise')),
       1 + (i.id%5), ROUND(i.total / (1 + (i.id%5)), 2)
FROM invoices i;

INSERT INTO payments (invoice_id, paid_on, amount, method)
SELECT i.id, DATE_ADD(i.issued_on, INTERVAL 1 + (i.id%25) DAY), i.total,
       ELT(1+(i.id*3)%4, 'card','ach','wire','credit')
FROM invoices i WHERE i.invoice_status = 'paid';

INSERT INTO feature_usage (account_id, feature_id, usage_date, event_count)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 4000)
SELECT 1 + (n*7)%200, 1 + (n*3)%6,
       DATE_SUB('2026-10-01', INTERVAL (n%180) DAY), 1 + (n*13)%500
FROM seq;

INSERT INTO tickets (account_id, opened_by, opened_at, closed_at, priority, ticket_status, subject)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 600)
SELECT u.account_id, u.id,
       TIMESTAMP(DATE_SUB('2026-10-01', INTERVAL (n*3)%400 DAY), SEC_TO_TIME((n*137)%86400)),
       IF((n*7)%5 = 0, NULL,
          TIMESTAMP(DATE_SUB('2026-10-01', INTERVAL ((n*3)%400) - (1+(n%9)) DAY), '17:00:00')),
       ELT(1+(n*5)%4, 'low','normal','high','urgent'),
       IF((n*7)%5 = 0, 'open', ELT(1+(n*3)%3, 'solved','closed','pending')),
       CONCAT(ELT(1+(n*11)%6,'Cannot','Slow','Error on','Question about','Billing issue with','Request to enable'),
              ' ', ELT(1+(n*13)%5,'login','export','webhooks','invoices','SSO'))
FROM seq JOIN users u ON u.id = 1 + (n*13)%900;

INSERT INTO ticket_messages (ticket_id, sent_at, from_support, body)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 3)
SELECT t.id, DATE_ADD(t.opened_at, INTERVAL seq.n HOUR), seq.n % 2,
       CONCAT('Message ', seq.n, ' on ticket ', t.id)
FROM tickets t JOIN seq ON seq.n <= 1 + (t.id % 3);

INSERT INTO audit_log (account_id, user_id, occurred_at, action, detail)
WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n < 8000)
SELECT 1 + (n*7)%200, IF((n*5)%13 = 0, NULL, 1 + (n*3)%900),
       TIMESTAMP(DATE_SUB('2026-10-01', INTERVAL (n%365) DAY), SEC_TO_TIME((n*97)%86400)),
       ELT(1+(n*11)%6,'login','invite_user','change_plan','export_data','rotate_key','delete_team'),
       CONCAT('ref=', n)
FROM seq;

ANALYZE TABLE accounts, users, teams, team_members, plans, subscriptions, invoices,
              invoice_lines, payments, features, plan_features, feature_usage,
              tickets, ticket_messages, audit_log;

-- =====================================================================
-- read-only grants for the agent on all three
-- =====================================================================
GRANT SELECT, SHOW VIEW ON hr_demo.*      TO 'agent_ro'@'%';
GRANT SELECT, SHOW VIEW ON library_demo.* TO 'agent_ro'@'%';
GRANT SELECT, SHOW VIEW ON saas_demo.*    TO 'agent_ro'@'%';
FLUSH PRIVILEGES;
