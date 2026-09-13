-- hrms-leave-agent 資料表與種子資料
-- 以「小時」計假（1 天 = 8 小時），才能處理半天假
-- 整份包在一個交易裡；重跑會完整重置所有資料（含 audit_logs）
-- 注意：外鍵要在「每個」連線上 PRAGMA foreign_keys = ON 才生效，應用程式連線時自己開

BEGIN;

CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    department TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leave_types (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leave_balances (
    employee_id TEXT NOT NULL REFERENCES employees(id),
    leave_type TEXT NOT NULL REFERENCES leave_types(code),
    year INTEGER NOT NULL,
    total_hours INTEGER NOT NULL CHECK (total_hours >= 0),
    used_hours INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (employee_id, leave_type, year),
    CHECK (used_hours >= 0 AND used_hours <= total_hours)
);

CREATE TABLE IF NOT EXISTS leave_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id TEXT NOT NULL REFERENCES employees(id),
    leave_type TEXT NOT NULL REFERENCES leave_types(code),
    start_at TEXT NOT NULL,  -- 固定格式 YYYY-MM-DDTHH:MM，字典序即時間序
    end_at TEXT NOT NULL,
    hours INTEGER NOT NULL CHECK (hours > 0),
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'SUBMITTED',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    detail TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 種子資料（先刪子表再刪父表）
DELETE FROM audit_logs;
DELETE FROM leave_requests;
DELETE FROM leave_balances;
DELETE FROM leave_types;
DELETE FROM employees;

INSERT INTO employees (id, name, department) VALUES
('E001', '張小明', '研發部'),
('E002', '李美玲', '行銷部'),
('E003', '王大衛', '研發部');

INSERT INTO leave_types (code, name) VALUES
('ANNUAL', '特休'),
('PERSONAL', '事假'),
('SICK', '病假');

INSERT INTO leave_balances (employee_id, leave_type, year, total_hours, used_hours) VALUES
('E001', 'ANNUAL', 2026, 80, 24),
('E001', 'PERSONAL', 2026, 112, 0),
('E001', 'SICK', 2026, 240, 8),
('E002', 'ANNUAL', 2026, 56, 56),
('E002', 'PERSONAL', 2026, 112, 16),
('E002', 'SICK', 2026, 240, 0),
('E003', 'ANNUAL', 2026, 112, 0),
('E003', 'PERSONAL', 2026, 112, 0),
('E003', 'SICK', 2026, 240, 0);

COMMIT;
