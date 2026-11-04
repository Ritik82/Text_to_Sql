# Simple and Robust Text-to-SQL Pipeline with LangGraph

#Imports
import os
import json
import re
import uuid
import mysql.connector
from typing import TypedDict, Annotated, List, Dict, Any, Optional
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# LangChain imports
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import  HumanMessage, AIMessage
from langchain_core.output_parsers import StrOutputParser

# LangGraph imports
from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver

# Utility imports
from prettytable import PrettyTable
from colorama import init, Fore, Style
from dotenv import load_dotenv
load_dotenv()

# Initialize colorama
init(autoreset=True)

# Runtime configuration is supplied by the Streamlit sidebar.
llm = None
db = None
app = None

# Database Manager
class DatabaseManager:
    """Simple database connection and query execution"""

    def __init__(self, config: dict):
        self.config = config
        self.connection = None
        self.schema_text = ""

    def connect(self):
        """Connect to MySQL database"""
        try:
            self.connection = mysql.connector.connect(**self.config)
            return True
        except Exception as e:
            print(f"{Fore.RED}Connection error: {e}")
            return False
        
    def get_schema(self) -> str:
        """Get simplified schema for LLM"""
        if not self.connection:
            self.connect()

        try:
            cursor = self.connection.cursor()
            cursor.execute("SHOW TABLES")
            tables = cursor.fetchall()

            schema_text = "DATABASE SCHEMA:\n\n"

            for (table_name,) in tables:
                schema_text += f"TABLE: {table_name}\n"

                # Get columns
                cursor.execute(f"DESCRIBE {table_name}")
                columns = cursor.fetchall()

                schema_text += "Columns:\n"
                for col in columns:
                    col_name, col_type, nullable, key, default, extra = col
                    schema_text += f"  - {col_name} ({col_type})"
                    if key == 'PRI':
                        schema_text += " PRIMARY KEY"
                    schema_text += "\n"

                # Get sample data (2 rows)
                cursor.execute(f"SELECT * FROM {table_name} LIMIT 2")
                samples = cursor.fetchall()
                if samples:
                    schema_text += "Sample rows:\n"
                    for row in samples:
                        schema_text += f"  {row}\n"
                schema_text += "\n"
            cursor.close()
            self.schema_text = schema_text
            return schema_text
        
        except Exception as e:
            print(f"{Fore.RED}Error fetching schema: {e}")
            return ""

    def execute_query(self, query: str) -> tuple:
        """Execute SQL query with auto-reconnect"""
        if not self.connection or not self.connection.is_connected():
            self.connect()

        try:
            cursor = self.connection.cursor()
            cursor.execute(query)

            # Check if query returns results (SELECT, SHOW, DESCRIBE, etc.)
            query_upper = query.strip().upper()
            if (query_upper.startswith('SELECT') or
                query_upper.startswith('SHOW') or
                query_upper.startswith('DESCRIBE') or
                query_upper.startswith('DESC') or
                query_upper.startswith('EXPLAIN')):

                results = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                cursor.close()
                return True, results, columns
            else:
                self.connection.commit()
                cursor.close()
                return True, "Query executed", None
        except Exception as e:
            # Reconnect and retry once
            if "Lost connection" in str(e) or "gone away" in str(e):
                self.connect()
                try:
                    cursor = self.connection.cursor()
                    cursor.execute(query)

                    query_upper = query.strip().upper()
                    if (query_upper.startswith('SELECT') or
                        query_upper.startswith('SHOW') or
                        query_upper.startswith('DESCRIBE') or
                        query_upper.startswith('DESC') or
                        query_upper.startswith('EXPLAIN')):

                        results = cursor.fetchall()
                        columns = [desc[0] for desc in cursor.description] if cursor.description else []
                        cursor.close()
                        return True, results, columns
                    else:
                        self.connection.commit()
                        cursor.close()
                        return True, "Query executed", None

                except Exception as e2:
                    return False, str(e2), None
            return False, str(e), None

# Agent State Definition
class AgentState(TypedDict):
    """Simple state for the pipeline"""
    messages: Annotated[List, add_messages]
    user_query: str
    schema: str
    sql_query: Optional[str]
    results: Optional[Dict]
    answer: Optional[str]
    error: Optional[str]
    retry_count: int

# Cell 5: Pipeline Nodes
def generate_sql_node(state: AgentState) -> Dict:
    """Generate SQL from natural language"""
    
    prompt = ChatPromptTemplate.from_template("""
You are a SQL expert. Generate a MySQL query for this request.

Database Schema:
{schema}

User Request: {query}

Rules:
1. Use ONLY tables and columns from the schema
2. Generate valid MySQL syntax
3. Use JOINs when needed
4. Return ONLY the SQL query, no explanations

SQL Query:""")

    chain = prompt | llm | StrOutputParser()
    
    try:
        sql = chain.invoke({
            "schema": state["schema"],
            "query": state["user_query"]
        })
        
        # Clean SQL
        sql = sql.strip()
        if sql.startswith("```"):
            sql = sql.split("```")[1].replace("sql", "").strip()

        if not is_sql_allowed_for_schema(sql, state["schema"]):
            return {
                "error": "The request cannot be answered using the current database schema.",
                "messages": [AIMessage(content="Query is outside the current schema")],
            }
            
        print(f"{Fore.YELLOW}Generated SQL: {sql}")

        return {
            "sql_query": sql,
            "error": None,
            "messages": [AIMessage(content=f"SQL generated")]
        }

    except Exception as e:
        return {
            "error": f"SQL generation failed: {str(e)}",
            "retry_count": state.get("retry_count", 0) + 1,
            "messages": [AIMessage(content=f"Error: {str(e)}")]
        }


def execute_sql_node(state: AgentState) -> Dict:
    """Execute SQL with retry logic"""

    if not state.get("sql_query"):
        return {
            "error": state.get("error") or "The model did not generate a SQL query.",
            "retry_count": state.get("retry_count", 0),
        }

    sql = state["sql_query"]
    retry_count = state.get("retry_count", 0)

    if not is_sql_allowed_for_schema(sql, state["schema"]):
        return {"error": "The generated SQL is outside the current database schema."}

    # Basic SQL injection check
    sql_upper = sql.upper()
    read_only_prefixes = ("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")
    if not sql_upper.startswith(read_only_prefixes):
        return {"error": "Unsafe SQL detected"}

    # Execute query
    success, data, columns = db.execute_query(sql)

    if not success and retry_count < 2:
    # If failed, try to fix and retry
        fix_prompt = ChatPromptTemplate.from_template("""
The following SQL query failed with error: {error}

Original SQL: {sql}

Database Schema:
{schema}

Generate a corrected SQL query that fixes the error. Return ONLY the SQL:""")

        chain = fix_prompt | llm | StrOutputParser()

        try:
            fixed_sql = chain.invoke({
                "error": data,
                "sql": sql,
                "schema": state["schema"]
            })

            # Clean fixed SQL
            fixed_sql = fixed_sql.strip()
            if fixed_sql.startswith("```"):
                fixed_sql = fixed_sql.split("```")[1].replace("sql", "").strip()

            if not is_sql_allowed_for_schema(fixed_sql, state["schema"]):
                return {
                    "error": "The corrected SQL is outside the current database schema.",
                    "retry_count": retry_count + 1,
                }

            print(f"{Fore.YELLOW}Retrying with fixed SQL: {fixed_sql}")

            # Try again with fixed SQL
            success, data, columns = db.execute_query(fixed_sql)

            if success:
                results = {
                    "columns": columns,
                    "rows": data,
                    "row_count": len(data)
                }
                return {
                    "results": results,
                    "sql_query": fixed_sql,
                    "error": None,
                    "retry_count": retry_count + 1,
                    "messages": [AIMessage(content=f"Query executed after retry")]
                }
        except:
            pass

    if success:
        results = {
            "columns": columns if columns else [],
            "rows": data if columns else [],
            "row_count": len(data) if columns else 0
        }

        print(f"{Fore.GREEN}Query executed: {results['row_count']} rows")

        return {
            "results": results,
            "error": None,
            "messages": [AIMessage(content=f"Query executed successfully")]
        }
    else:
        return {
            "error": f"Execution failed: {data}",
            "retry_count": retry_count + 1,
            "messages": [AIMessage(content=f"Error: {data}")]
        }


def format_answer_node(state: AgentState) -> Dict:
    """Format results as natural language"""
    
    if state.get("error"):
        return {
            "answer": f"I encountered an error: {state['error']}",
            "messages": [AIMessage(content="Error in processing")]
        }
        
    if not state.get("results"):
        return {
            "answer": "No results to display",
            "messages": [AIMessage(content="No results")]
        }
        
    results = state["results"]
    
    # Prepare data for LLM
    if results["rows"]:
        # Format first 10 rows
        rows_text = f"Columns: {results['columns']}\n"
        for i, row in enumerate(results["rows"][:10]):
            rows_text += f"Row {i+1}: {row}\n"
        if results["row_count"] > 10:
            rows_text += f"... and {results['row_count'] - 10} more rows"
    else:
        rows_text = "No data found"

    prompt = ChatPromptTemplate.from_template("""
Convert these SQL results into a clear answer for the user.

User Question: {question}
SQL Query: {sql}
Results ({count} rows):
{results}

Provide a concise, informative answer:""")

    chain = prompt | llm | StrOutputParser()

    try:
        answer = chain.invoke({
            "question": state["user_query"],
            "sql": state["sql_query"],
            "results": rows_text,
            "count": results["row_count"]
        })
        
        return {
            "answer": answer,
            "messages": [AIMessage(content="Answer formatted")]
        }
    except Exception as e:
        return {
            "answer": f"Results: {results['row_count']} rows found",
            "messages": [AIMessage(content="Basic answer provided")]
        }


def schema_objects(schema: str) -> tuple:
    """Return the table and column names available in the loaded schema."""
    tables = set(re.findall(r"^TABLE:\s+([\w]+)", schema, re.MULTILINE | re.IGNORECASE))
    columns = set(re.findall(r"^\s+-\s+([\w]+)\s+\(", schema, re.MULTILINE))
    return {name.lower() for name in tables}, {name.lower() for name in columns}


def is_sql_allowed_for_schema(sql: str, schema: str) -> bool:
    """Reject non-read-only SQL and references to unknown tables."""
    if not sql:
        return False

    tables, _ = schema_objects(schema)
    sql_upper = sql.strip().upper()
    if not sql_upper.startswith(("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")):
        return False

    referenced_tables = re.findall(
        r"\b(?:FROM|JOIN|UPDATE|INTO|TABLE|DESCRIBE|DESC)\s+[`\"]?([\w]+)",
        sql,
        re.IGNORECASE,
    )
    if sql_upper.startswith("SHOW TABLES"):
        return bool(tables)
    return bool(referenced_tables) and bool(tables) and all(
        table.lower() in tables for table in referenced_tables
    )


def validate_query(query: str, llm, schema: str) -> tuple:
    """
    Validates if query should go through SQL pipeline
    Returns: (is_sql_query: bool, response: str)
    """
    validation_prompt = ChatPromptTemplate.from_template("""
You are a strict gatekeeper for a SQL database query system. Analyze the user's input only against the current schema.

User Input: {query}

Current Database Schema:
{schema}

STRICT RULES:
1. Return ONLY TRUE if the request can be answered using tables and columns in the current schema.
2. Return ONLY OUT_OF_SCOPE for greetings, general chat, gibberish, questions about another subject, or requests requiring tables/columns not in the current schema.

Examples of in-scope database queries (return "TRUE"):
- Show me all customers (only if customers exists in the current schema)
- Count the records
- List all tables

Examples of out-of-scope inputs (return "OUT_OF_SCOPE"):
- Hello/Hi
- What's 2+2?
- Write Python code
- Show students, when there is no students table in the current schema

Never answer the user, explain your decision, or use information outside the current schema.

Response:""")

    chain = validation_prompt | llm | StrOutputParser()

    try:
        result = chain.invoke({"query": query, "schema": schema})
        result = result.strip()

        # Use regex to check if response is exactly TRUE (with possible whitespace)
        true_pattern = re.compile(r'^TRUE$', re.IGNORECASE)

        # Check if the response contains only TRUE
        if true_pattern.match(result):
            return True, None
        elif "TRUE" in result.upper() and len(result) < 10:
            # Handle cases where LLM might return "TRUE." or "TRUE\n"
            cleaned = re.sub(r'[^\w]', '', result).upper()
            if cleaned == "TRUE":
                return True, None

        return False, "I can only answer questions related to the current database schema."

    except Exception as e:
        print(f"{Fore.YELLOW}Validation error: {e}")
        return False, "I could not validate that request against the current database schema."

    

# Build Graph
def create_graph():
    """Create the LangGraph workflow"""
    
    workflow = StateGraph(AgentState)
    
    # Add nodes
    workflow.add_node("generate_sql", generate_sql_node)
    workflow.add_node("execute_sql", execute_sql_node)
    workflow.add_node("format_answer", format_answer_node)
    
    # Define flow
    workflow.add_edge(START, "generate_sql")
    workflow.add_edge("generate_sql", "execute_sql")
    
    workflow.add_edge("execute_sql", "format_answer")
    workflow.add_edge("format_answer", END)

    # Compile
    memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)

    return app

def configure_llm(groq_api_key: str):
    """Configure the language model from a user-provided API key."""
    global llm

    candidate_llm = ChatGroq(
        model="openai/gpt-oss-20b",
        temperature=0.0,
        max_tokens=2048,
        groq_api_key=groq_api_key,
    )

    # Make a small request so the key is verified before continuing.
    candidate_llm.invoke("Reply with exactly: connected")
    llm = candidate_llm
    return llm


def configure_database(mysql_config: dict):
    """Connect to MySQL and load the schema supplied to the LLM."""
    global db, app

    candidate_db = DatabaseManager(mysql_config)
    if not candidate_db.connect():
        raise ConnectionError("Could not connect to the MySQL database.")

    candidate_db.get_schema()
    if not candidate_db.schema_text:
        raise ConnectionError("Connected to MySQL, but the database schema could not be read.")

    db = candidate_db
    app = create_graph()
    return db, app


def configure_runtime(groq_api_key: str, mysql_config: dict) -> tuple:
    """Configure both the language model and database."""
    configure_llm(groq_api_key)
    return configure_database(mysql_config)

# Cell 7: Main Execution Function
def run_query(question: str) -> Dict:
    """Execute a natural language query"""

    if llm is None or db is None or app is None:
        return {
            "answer": "Please connect the LLM and database before running a query.",
            "sql_query": None,
            "results": None,
            "is_sql": False,
        }

    print(f"\n{Fore.CYAN}{'='*60}")
    print(f"{Fore.CYAN}Query: {question}")
    print(f"{Fore.CYAN}{'='*60}\n")

    # VALIDATION LAYER
    print(f"{Fore.YELLOW}Validating query...")
    is_sql_query, response_text = validate_query(question, llm, db.schema_text)

    if not is_sql_query:
        # Keep out-of-scope input from becoming a general chatbot response.
        print(f"\n{Fore.BLUE}{'='*60}")
        print(f"{Fore.BLUE}RESPONSE:")
        print(f"{Fore.BLUE}{'='*60}")
        print(f"{Fore.WHITE}{response_text}\n")
        return {
            "answer": response_text,
            "sql_query": None,
            "results": None,
            "is_sql": False
        }

    print(f"{Fore.GREEN}✓ SQL query validated, processing...\n")
    # END VALIDATION

    # Initialize state
    initial_state = {
        "messages": [HumanMessage(content=question)],
        "user_query": question,
        "schema": db.schema_text,
        "retry_count": 0
    }

    # Run the graph
    config = {"configurable": {"thread_id": f"query_{uuid.uuid4()}"}}

    try:
        # Execute workflow
        for step in app.stream(initial_state, config):
            for key, value in step.items():
                print(f"{Fore.YELLOW}Step: {key}")

        # Get final state
        final_state = app.get_state(config).values
        final_state["is_sql"] = True  # Add flag

        # Display results
        if final_state.get("answer"):
            print(f"\n{Fore.GREEN}{'='*60}")
            print(f"{Fore.GREEN}ANSWER:")
            print(f"{Fore.GREEN}{'='*60}")
            print(f"{Fore.WHITE}{final_state['answer']}\n")

        # Show table if results exist
        if final_state.get("results") and final_state["results"]["rows"]:
            results = final_state["results"]
            table = PrettyTable()
            table.field_names = results["columns"]

            # Show first 10 rows
            for row in results["rows"][:10]:
                table.add_row(row)

            print(f"\n{Fore.CYAN}Results Table:")
            print(table)

            if results["row_count"] > 10:
                print(f"{Fore.YELLOW}Showing 10 of {results['row_count']} rows")
        return final_state

    except Exception as e:
        print(f"{Fore.RED}Error: {str(e)}")
        return {"error": str(e)}

# Cell 9: Interactive Query Loop (Optional)
def interactive_mode():
    """Run queries interactively"""
    print(f"\n{Fore.CYAN}Interactive Text-to-SQL Mode")
    print(f"{Fore.CYAN}Type 'exit' to quit\n")
    
    while True:
        query = input(f"{Fore.YELLOW}Enter your question: ")
        
        if query.lower() == 'exit':
            break
            
        if query.strip():
            run_query(query)
            
        print("\n" + "="*60 + "\n")




