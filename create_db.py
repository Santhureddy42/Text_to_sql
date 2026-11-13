"""Create a sample company.db SQLite database for the Text-to-SQL app."""

import sqlite3
from contextlib import closing
from pathlib import Path


DATABASE_PATH = Path(__file__).resolve().with_name("company.db")

EMPLOYEES = [
    (1, "Ava Martinez", "Engineering", 92000, "Seattle", 7),
    (2, "Liam Chen", "Engineering", 108000, "San Francisco", 10),
    (3, "Noah Patel", "Engineering", 76000, "Austin", 4),
    (4, "Mia Robinson", "Engineering", 84000, "Chicago", 6),
    (5, "Ethan Kim", "Sales", 68000, "New York", 5),
    (6, "Sophia Brooks", "Sales", 73500, "Boston", 8),
    (7, "Lucas Garcia", "Sales", 59000, "Austin", 3),
    (8, "Isabella Nguyen", "Marketing", 72000, "Seattle", 6),
    (9, "Mason Wilson", "Marketing", 64000, "Denver", 4),
    (10, "Amelia Foster", "Marketing", 81000, "New York", 9),
    (11, "James Okafor", "Human Resources", 67000, "Chicago", 7),
    (12, "Charlotte Davis", "Human Resources", 62000, "Boston", 4),
    (13, "Benjamin Lee", "Finance", 88000, "New York", 8),
    (14, "Harper Singh", "Finance", 95000, "San Francisco", 11),
    (15, "Elijah Thompson", "Finance", 71000, "Denver", 5),
    (16, "Evelyn Clark", "Operations", 69000, "Austin", 6),
    (17, "Logan Rivera", "Operations", 78000, "Seattle", 9),
    (18, "Abigail Moore", "Customer Support", 54000, "Chicago", 2),
    (19, "Daniel Park", "Customer Support", 58000, "Boston", 3),
    (20, "Emily Johnson", "Engineering", 101000, "Denver", 8),
]


def create_database(db_path: str | Path = DATABASE_PATH) -> None:
    """Create the employees table and insert the sample employee records."""
    with closing(sqlite3.connect(db_path)) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id INTEGER PRIMARY KEY,
                    employee_name TEXT,
                    department TEXT,
                    salary REAL,
                    city TEXT,
                    experience INTEGER
                )
                """
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO employees
                    (employee_id, employee_name, department, salary, city, experience)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                EMPLOYEES,
            )
    print(f"Database created successfully: {db_path}")


if __name__ == "__main__":
    create_database()
