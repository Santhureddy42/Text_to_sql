# Text-to-SQL Using Python, Gemini API and SQLite

A beginner-friendly command-line application that turns English questions
about a sample employee database into read-only SQLite queries.

## Project files

- [`.env`](./.env) — local Gemini API key and optional model setting. This file
  is ignored by Git; add your own key locally.
- [`.gitignore`](./.gitignore) — excludes the virtual environment, local
  configuration, generated database, and Python cache files.
- [`requirements.txt`](./requirements.txt) — the Google GenAI SDK,
  `python-dotenv`, HTTP timeout support, and console formatting/color packages.
- [`create_db.py`](./create_db.py) — creates `company.db`, the `employees`
  table, and 20 sample employee records.
- [`text_to_sql.py`](./text_to_sql.py) — reads the schema, asks Gemini for SQL,
  validates that it is read-only, and displays query results in a table.

## Configure the Gemini API key

1. Get an API key from Google AI Studio.
2. Open `.env` in VS Code.
3. Set the value without quotes:

   ```text
   GEMINI_API_KEY=your_actual_gemini_api_key
   GEMINI_MODEL=gemini-3.8-flash
   GEMINI_FALLBACK_MODEL=gemini-3.5-flash-lite
   ```

   Do not share or commit your API key. The fallback is attempted once only
   after a primary HTTP 503/504 or request timeout. Invalid keys, permission
   errors, and HTTP 404 model-not-found errors do not trigger fallback.

## Console commands

The application displays the database schema at startup, including each
column's declared type and primary-key status. At the prompt:

- Enter a natural-language question to generate and run a read-only query.
- Enter `schema` to display the schema again without calling Gemini.
- Enter `exit` or `quit` to close the application.

Results and schema are formatted as tables, with colored headings, SQL,
success messages, and errors. Each query also reports model and execution
timings.

## Install and run in the VS Code terminal

Open the project folder in VS Code, then open **Terminal > New Terminal** and
run:

```powershell
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python create_db.py
python text_to_sql.py
```

If PowerShell prevents virtual-environment activation, run the commands using
the interpreter directly:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe create_db.py
.\venv\Scripts\python.exe text_to_sql.py
```

Ask a question such as `Display employees earning more than 50000.` The
application prints the generated SQL and its results. Enter another question
to continue, or type `exit` to quit.

The database connection used by the application is read-only. SQL is checked
before execution, and the SQLite authorizer rejects writes and other
disallowed operations. Run `python create_db.py` again to add any missing
sample records if needed.

The application reuses its read-only database connection, sends one Gemini
request per question with retries disabled and a 25-second timeout, and prints
API, SQL, and total processing times after each question.
