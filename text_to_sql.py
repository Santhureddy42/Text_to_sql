"""Interactive, read-only Text-to-SQL application powered by Google Gemini."""

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
from colorama import Fore, Style, init
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from tabulate import tabulate


DATABASE_PATH = Path(__file__).resolve().with_name("company.db")
API_TIMEOUT_MS = 25_000
MAX_SQL_TOKENS = 128

READ_ONLY_SQLITE_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    READ_ONLY_SQLITE_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)


def get_read_only_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite database without allowing database writes."""
    database_uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    return sqlite3.connect(database_uri, uri=True)


def _extract_schema_from_connection(
    connection: sqlite3.Connection,
) -> dict[str, list[str]]:
    return {
        table_name: [column_name for column_name, _, _ in columns]
        for table_name, columns in _extract_schema_details_from_connection(
            connection
        ).items()
    }


def _extract_schema_details_from_connection(
    connection: sqlite3.Connection,
) -> dict[str, list[tuple[str, str, bool]]]:
    """Return column names, declared types, and primary-key flags per table."""
    schema: dict[str, list[str]] = {}
    tables = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for (table_name,) in tables:
        escaped_name = table_name.replace('"', '""')
        columns = connection.execute(
            f'PRAGMA table_info("{escaped_name}")'
        ).fetchall()
        schema[table_name] = [
            (column[1], column[2] or "UNSPECIFIED", column[5] > 0)
            for column in columns
        ]
    return schema


def extract_schema_details(
    connection: sqlite3.Connection,
) -> dict[str, list[tuple[str, str, bool]]]:
    """Get schema metadata from an existing SQLite connection."""
    return _extract_schema_details_from_connection(connection)


def extract_schema(
    db_path: str | Path = DATABASE_PATH,
    connection: sqlite3.Connection | None = None,
) -> dict[str, list[str]]:
    """Return table names and columns, reusing a provided connection if available."""
    if connection is not None:
        return _extract_schema_from_connection(connection)
    with closing(get_read_only_connection(db_path)) as read_connection:
        return _extract_schema_from_connection(read_connection)


def generate_sql(
    question: str,
    schema: dict[str, list[str]],
    client: genai.Client,
    model: str,
) -> str:
    """Ask Gemini for a single SQLite SELECT query without Markdown fences."""
    schema_text = json.dumps(schema, separators=(",", ":"))
    response = client.models.generate_content(
        model=model,
        contents=f"Schema: {schema_text}\nQuestion: {question}",
        config=types.GenerateContentConfig(
            system_instruction=(
                "Using only this schema, return one SQLite SELECT or read-only "
                "WITH query. Return SQL only, without explanations or Markdown."
            ),
            candidate_count=1,
            max_output_tokens=MAX_SQL_TOKENS,
        ),
    )

    sql = response.text
    if not sql or not sql.strip():
        raise ValueError("Gemini returned an empty SQL response.")
    return sql.strip()


def _api_error_status(error: genai_errors.APIError) -> int | None:
    """Return the HTTP status code when the SDK provides one."""
    try:
        return int(error.code)
    except (AttributeError, TypeError, ValueError):
        return None


def _timed_generate_sql(
    question: str,
    schema: dict[str, list[str]],
    client: genai.Client,
    model: str,
    label: str,
    timings: dict[str, float],
) -> str:
    started = time.perf_counter()
    try:
        return generate_sql(question, schema, client, model)
    finally:
        timings[label] = time.perf_counter() - started


def generate_sql_with_fallback(
    question: str,
    schema: dict[str, list[str]],
    client: genai.Client,
    primary_model: str,
    fallback_model: str,
    timings: dict[str, float],
) -> str:
    """Use the fallback once for transient primary failures only."""
    primary_failure: str
    try:
        return _timed_generate_sql(
            question, schema, client, primary_model, "primary", timings
        )
    except genai_errors.APIError as primary_error:
        status_code = _api_error_status(primary_error)
        if status_code not in (503, 504):
            raise
        primary_failure = f"HTTP {status_code}"
    except httpx.TimeoutException:
        primary_failure = "a connection timeout"

    if not fallback_model:
        raise RuntimeError(
            f"The primary model failed with {primary_failure}, but no fallback "
            "is configured. Set GEMINI_FALLBACK_MODEL in .env."
        )
    if fallback_model == primary_model:
        raise RuntimeError(
            f"The primary model failed with {primary_failure}, but "
            "GEMINI_FALLBACK_MODEL is the same model. Configure a different "
            "fallback model."
        )

    try:
        return _timed_generate_sql(
            question, schema, client, fallback_model, "fallback", timings
        )
    except genai_errors.APIError as fallback_error:
        status_code = _api_error_status(fallback_error)
        if status_code == 404:
            message = (
                f"The primary model failed with {primary_failure}, and fallback "
                f"model '{fallback_model}' was not found (HTTP 404). Check "
                "GEMINI_FALLBACK_MODEL."
            )
        elif status_code in (503, 504):
            message = (
                f"The primary model failed with {primary_failure}, and the "
                f"fallback model returned HTTP {status_code}. Try again later "
                "or configure different available models."
            )
        elif status_code in (401, 403):
            message = (
                f"The primary model failed with {primary_failure}, and the "
                f"fallback request failed with HTTP {status_code}. Check API "
                "credentials and access."
            )
        else:
            status_text = (
                f"HTTP {status_code}" if status_code is not None else "an API error"
            )
            message = (
                f"The primary model failed with {primary_failure}, and the "
                f"fallback request failed with {status_text}."
            )
        raise RuntimeError(message) from fallback_error
    except httpx.TimeoutException as fallback_error:
        raise RuntimeError(
            f"The primary model failed with {primary_failure}, and the fallback "
            "request timed out."
        ) from fallback_error
    except httpx.HTTPError as fallback_error:
        raise RuntimeError(
            f"The primary model failed with {primary_failure}, but the fallback "
            "request could not reach Gemini."
        ) from fallback_error
    except ValueError as fallback_error:
        raise RuntimeError(
            f"The primary model failed with {primary_failure}, and the fallback "
            "returned no usable SQL."
        ) from fallback_error


def _read_only_authorizer(
    action: int,
    first_argument: str | None,
    second_argument: str | None,
    *_: Any,
) -> int:
    """Allow query compilation and reads, but deny writes and other operations."""
    if action == sqlite3.SQLITE_FUNCTION:
        function_name = (second_argument or first_argument or "").lower()
        if function_name == "load_extension":
            return sqlite3.SQLITE_DENY

    if action in READ_ONLY_SQLITE_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def validate_sql(connection: sqlite3.Connection, query: str) -> None:
    """Check that SQL is a single SELECT/WITH query and can compile safely."""
    if not query or not query.strip():
        raise ValueError("The generated SQL query is empty.")

    starts_with_select_or_with = re.match(
        r"\A(?:\s|--[^\r\n]*(?:\r?\n|$)|/\*.*?\*/)*"
        r"(SELECT|WITH)\b",
        query,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if starts_with_select_or_with is None:
        raise ValueError("Only SELECT queries and read-only WITH queries are allowed.")

    connection.set_authorizer(_read_only_authorizer)
    try:
        # EXPLAIN checks the SQL without running the requested query.
        connection.execute(f"EXPLAIN {query}").fetchall()
    except (sqlite3.Error, sqlite3.Warning) as error:
        raise ValueError(f"SQL is invalid or not allowed: {error}") from error


def _execute_on_connection(
    connection: sqlite3.Connection,
    query: str,
) -> list[dict[str, object]]:
    validate_sql(connection, query)
    connection.row_factory = sqlite3.Row
    cursor = connection.execute(query)
    if cursor.description is None:
        raise ValueError("The SQL query did not return any columns.")
    return [dict(row) for row in cursor.fetchall()]


def execute_query(
    db_path: str | Path,
    query: str,
    connection: sqlite3.Connection | None = None,
) -> list[dict[str, object]]:
    """Validate and execute one read-only query, reusing a connection if given."""
    if connection is not None:
        return _execute_on_connection(connection, query)
    with closing(get_read_only_connection(db_path)) as read_connection:
        return _execute_on_connection(read_connection, query)


def display_results(rows: list[dict[str, object]]) -> None:
    """Print query results in a readable, colored table."""
    if not rows:
        _print_color("No matching rows.", Fore.YELLOW)
        _print_color("Rows returned: 0", Fore.GREEN)
        return

    printable_rows = [
        {
            column: "NULL" if value is None else value
            for column, value in row.items()
        }
        for row in rows
    ]
    _print_color(
        tabulate(printable_rows, headers="keys", tablefmt="grid"),
        Fore.GREEN,
    )
    _print_color(f"Rows returned: {len(rows)}", Fore.GREEN)


def display_schema(
    schema_details: dict[str, list[tuple[str, str, bool]]],
) -> None:
    """Display table and column metadata without making an API request."""
    _print_color("\nDatabase schema", Fore.CYAN, bright=True)
    if not schema_details:
        _print_color("No tables found.", Fore.YELLOW)
        return

    for table_name, columns in schema_details.items():
        _print_color(f"\nTable: {table_name}", Fore.CYAN, bright=True)
        table_rows = [
            {
                "Column": name,
                "Data type": data_type,
                "Primary key": "Yes" if is_primary_key else "No",
            }
            for name, data_type, is_primary_key in columns
        ]
        _print_color(
            tabulate(table_rows, headers="keys", tablefmt="grid"),
            Fore.WHITE,
        )


def _print_color(message: str, color: str, bright: bool = False) -> None:
    style = Style.BRIGHT if bright else ""
    print(f"{color}{style}{message}{Style.RESET_ALL}")


def _report_api_error(error: genai_errors.APIError, model: str) -> None:
    """Show a useful API error without printing request details or credentials."""
    status_code = _api_error_status(error)
    if status_code == 404:
        _print_color(
            f"Primary model '{model}' was not found (HTTP 404). "
            "Check GEMINI_MODEL.",
            Fore.RED,
        )
    elif status_code in (401, 403):
        _print_color(
            "Gemini authentication failed. Check GEMINI_API_KEY and API access.",
            Fore.RED,
        )
    elif status_code in (503, 504):
        _print_color(
            f"Primary model '{model}' is temporarily unavailable "
            f"(HTTP {status_code}).",
            Fore.RED,
        )
    elif status_code is not None:
        _print_color(
            f"Gemini API error (HTTP {status_code}) for the primary model. "
            "Check the model and try again.",
            Fore.RED,
        )
    else:
        _print_color(
            "Gemini API request failed. Check your connection and model setting.",
            Fore.RED,
        )


def main() -> None:
    """Load configuration and run the interactive question loop."""
    init(autoreset=True)
    load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"))
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        _print_color(
            "Configuration error: GEMINI_API_KEY is missing. "
            "Add your key to the .env file and restart the application.",
            Fore.RED,
        )
        return

    if not DATABASE_PATH.is_file():
        _print_color("Database not found. Run create_db.py first.", Fore.RED)
        return

    primary_model = os.getenv("GEMINI_MODEL", "").strip()
    fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
    if not primary_model or not fallback_model:
        _print_color(
            "Configuration error: set both GEMINI_MODEL and "
            "GEMINI_FALLBACK_MODEL in .env.",
            Fore.RED,
        )
        return
    try:
        with closing(get_read_only_connection(DATABASE_PATH)) as db_connection:
            db_connection.row_factory = sqlite3.Row
            schema_details = extract_schema_details(db_connection)
            schema = {
                table: [column_name for column_name, _, _ in columns]
                for table, columns in schema_details.items()
            }
            if not schema:
                _print_color("The database does not contain any tables.", Fore.RED)
                return

            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(
                    timeout=API_TIMEOUT_MS,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
            _print_color(
                "Welcome to Text-to-SQL using Gemini AI!",
                Fore.CYAN,
                bright=True,
            )
            _print_color("Available commands:", Fore.YELLOW)
            _print_color("  schema - Display database schema", Fore.YELLOW)
            _print_color("  exit   - Exit the application", Fore.YELLOW)
            _print_color("  quit   - Exit the application", Fore.YELLOW)
            display_schema(schema_details)

            while True:
                try:
                    question = input(
                        f"\n{Fore.WHITE}Enter your question: {Style.RESET_ALL}"
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    _print_color("\nGoodbye!", Fore.YELLOW)
                    break

                command = question.casefold()
                if command in {"exit", "quit"}:
                    _print_color("Goodbye!", Fore.YELLOW)
                    break
                if not question:
                    _print_color(
                        "Please enter a question or a command.",
                        Fore.YELLOW,
                    )
                    continue
                if command == "schema":
                    display_schema(schema_details)
                    continue

                total_started = time.perf_counter()
                model_timings: dict[str, float] = {}
                sql_elapsed = 0.0
                try:
                    query = generate_sql_with_fallback(
                        question,
                        schema,
                        client,
                        primary_model,
                        fallback_model,
                        model_timings,
                    )
                    _print_color(
                        f"\nGenerated SQL:\n{query}",
                        Fore.CYAN,
                        bright=True,
                    )

                    sql_started = time.perf_counter()
                    try:
                        rows = execute_query(
                            DATABASE_PATH,
                            query,
                            connection=db_connection,
                        )
                    finally:
                        sql_elapsed = time.perf_counter() - sql_started
                    _print_color("\nQuery Results", Fore.CYAN, bright=True)
                    display_results(rows)
                except KeyboardInterrupt:
                    _print_color("\nInterrupted. Goodbye!", Fore.YELLOW)
                    break
                except httpx.TimeoutException:
                    _print_color(
                        "Gemini request timed out after 25 seconds. Please try again.",
                        Fore.RED,
                    )
                except httpx.HTTPError:
                    _print_color(
                        "Could not reach Gemini. Check your network connection.",
                        Fore.RED,
                    )
                except genai_errors.APIError as error:
                    _report_api_error(error, primary_model)
                except RuntimeError as error:
                    _print_color(str(error), Fore.RED)
                except (ValueError, sqlite3.Error) as error:
                    _print_color(f"Could not run the generated query: {error}", Fore.RED)
                finally:
                    total_elapsed = time.perf_counter() - total_started
                    primary_elapsed = model_timings.get("primary")
                    fallback_elapsed = model_timings.get("fallback")
                    if primary_elapsed is None:
                        _print_color(
                            "Primary model response time: not available",
                            Fore.WHITE,
                        )
                    else:
                        _print_color(
                            f"Primary model response time: "
                            f"{primary_elapsed:.3f} seconds",
                            Fore.WHITE,
                        )
                    if fallback_elapsed is None:
                        _print_color(
                            "Fallback model response time: not attempted",
                            Fore.WHITE,
                        )
                    else:
                        _print_color(
                            f"Fallback model response time: "
                            f"{fallback_elapsed:.3f} seconds",
                            Fore.WHITE,
                        )
                    api_elapsed = sum(model_timings.values())
                    _print_color(
                        f"Gemini API response time: {api_elapsed:.3f} seconds",
                        Fore.WHITE,
                    )
                    _print_color(
                        f"SQL execution time: {sql_elapsed:.3f} seconds",
                        Fore.WHITE,
                    )
                    _print_color(
                        f"Total processing time: {total_elapsed:.3f} seconds",
                        Fore.WHITE,
                    )
    except sqlite3.Error:
        _print_color(
            "Could not read company.db. Check that the database is valid.",
            Fore.RED,
        )
    except httpx.TimeoutException:
        _print_color("Timed out while connecting to Gemini. Please try again.", Fore.RED)
    except httpx.HTTPError:
        _print_color(
            "Could not initialize the Gemini connection. Check your network.",
            Fore.RED,
        )
    except genai_errors.APIError as error:
        _report_api_error(error, primary_model)


if __name__ == "__main__":
    main()
