import streamlit as st
import csv
import io
import os

import agent


st.set_page_config(page_title="Text to SQL", page_icon="SQL", layout="wide")

if "llm_configured" not in st.session_state:
	st.session_state.llm_configured = False
if "db_configured" not in st.session_state:
	st.session_state.db_configured = False
if "last_result" not in st.session_state:
	st.session_state.last_result = None


with st.sidebar:
	st.header("Connection setup")
	st.caption("Credentials are used only for this running app session.")

	with st.form("llm_form"):
		groq_api_key = st.text_input(
			"Groq API key",
			value="",
			type="password",
		)
		llm_clicked = st.form_submit_button("Connect LLM", type="primary", use_container_width=True)

	if llm_clicked:
		if not groq_api_key:
			st.error("Enter a Groq API key.")
			st.session_state.llm_configured = False
		else:
			try:
				agent.configure_llm(groq_api_key)
				st.session_state.llm_configured = True
			except Exception as error:
				st.session_state.llm_configured = False
				st.error(f"LLM connection failed: {error}")

	if st.session_state.llm_configured:
		st.success("LLM configured")

	st.divider()
	with st.form("database_form"):
		st.subheader("MySQL database")
		mysql_host = st.text_input("Host", value="localhost")
		mysql_port = st.number_input("Port", min_value=1, max_value=65535, value=3306)
		mysql_database = st.text_input("Database")
		mysql_user = st.text_input("Username")
		mysql_password = st.text_input("Password", type="password")
		db_clicked = st.form_submit_button("Connect database", type="primary", use_container_width=True)

	if db_clicked:
		if not st.session_state.llm_configured:
			st.error("Connect the LLM before connecting the database.")
			st.session_state.db_configured = False
		elif not mysql_host or not mysql_database or not mysql_user:
			st.error("Enter all required MySQL fields.")
			st.session_state.db_configured = False
		else:
			try:
				agent.configure_database(
					{
						"host": mysql_host,
						"port": int(mysql_port),
						"database": mysql_database,
						"user": mysql_user,
						"password": mysql_password,
					},
				)
				st.session_state.db_configured = True
				st.session_state.last_result = None
			except Exception as error:
				st.session_state.db_configured = False
				st.error(f"Connection failed: {error}")

	if st.session_state.db_configured:
		st.success("Database connected")
		st.subheader("Database schema")
		if agent.db is not None:
			st.code(agent.db.schema_text, language="text")


st.title("Text to SQL")
st.write("Ask a question about the connected MySQL database in plain English.")

if not st.session_state.llm_configured:
	st.info("Connect the LLM in the sidebar to get started.")
elif not st.session_state.db_configured:
	st.info("Connect the database in the sidebar to start querying.")
else:
	with st.form("query_form"):
		question = st.text_area(
			"Your question",
			placeholder="For example: Show the five customers with the highest credit limit",
			height=100,
		)
		query_clicked = st.form_submit_button("Run query", type="primary")

	if query_clicked:
		if not question.strip():
			st.warning("Enter a question first.")
		else:
			with st.spinner("Generating SQL and fetching results..."):
				st.session_state.last_result = agent.run_query(question.strip())

	result = st.session_state.last_result
	if result:
		if result.get("answer"):
			st.subheader("Answer")
			st.write(result["answer"])

		if result.get("sql_query"):
			st.subheader("SQL query")
			st.code(result["sql_query"], language="sql")

		query_results = result.get("results")
		if query_results and query_results.get("columns"):
			rows = query_results.get("rows", [])
			columns = query_results["columns"]
			st.subheader(f"Data ({query_results.get('row_count', len(rows))} rows)")
			display_data = (
				[dict(zip(columns, row)) for row in rows]
				if rows
				else {column: [] for column in columns}
			)
			st.dataframe(display_data, use_container_width=True)

			csv_buffer = io.StringIO()
			writer = csv.writer(csv_buffer)
			writer.writerow(columns)
			writer.writerows(rows)
			st.download_button(
				"Download data as CSV",
				data=csv_buffer.getvalue(),
				file_name="query_results.csv",
				mime="text/csv",
			)

