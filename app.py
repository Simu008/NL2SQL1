import os
import psycopg2
from psycopg2 import sql
import pandas as pd
import streamlit as st
from typing import List, Tuple, Dict
from openai import OpenAI

# Replace the hardcoded values with environment variables
DB_NAME = os.getenv("DB_NAME", "Sample")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root")
DB_HOST = os.getenv("DB_HOST", "db")  # Changed from localhost to db
DB_PORT = os.getenv("DB_PORT", "5432")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")

SCHEMA = """
Table: department
Columns: 
- id (INTEGER, Primary Key)
- name (VARCHAR(50))
- location (VARCHAR(100))
- budget (DECIMAL(15,2))
Sample values: Engineering in Building A, Marketing in Building B, Sales in Building C

Table: employee
Columns:
- id (INTEGER, Primary Key)
- name (VARCHAR(100))
- salary (DECIMAL(10,2))
- department_id (INTEGER, Foreign Key to department.id)
- email (VARCHAR(100))
- hire_date (DATE)
Salary range: 85,000 to 160,000

Table: project
Columns:
- id (INTEGER, Primary Key)
- name (VARCHAR(100))
- start_date (DATE)
- end_date (DATE)
- department_id (INTEGER, Foreign Key to department.id)
Sample projects: Mobile App Development, Q4 Marketing Campaign

Table: employee_project
Columns:
- employee_id (INTEGER, Foreign Key to employee.id)
- project_id (INTEGER, Foreign Key to project.id)
- role (VARCHAR(50))

Relationships:
- employee.department_id references department.id
- project.department_id references department.id
- employee_project.employee_id references employee.id
- employee_project.project_id references project.id
"""

class Database:
    def __init__(self):
        try:
            self.conn = psycopg2.connect(
                dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
                host=DB_HOST, port=DB_PORT
            )
            self.cur = self.conn.cursor()
        except psycopg2.Error as e:
            raise Exception(f"Database connection failed: {str(e)}")

    def execute_query(self, query: str) -> Tuple[List[tuple], List[str]]:
        try:
            self.cur.execute(query)
            rows = self.cur.fetchall()
            column_names = [desc[0] for desc in self.cur.description]
            return rows, column_names
        except psycopg2.Error as e:
            raise Exception(f"Query execution failed: {str(e)}")

    def close(self):
        if hasattr(self, 'cur') and self.cur:
            self.cur.close()
        if hasattr(self, 'conn') and self.conn:
            self.conn.close()

@st.cache_resource
class OpenAILLM:
    def __init__(self):
        self.client = OpenAI(api_key=OPENAI_API_KEY)
        self.error_context = []

    def expand_query(self, query: str) -> str:
        """Expand user query with schema context"""
        response = self.client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": f"You are an expert at reformulating database queries.\n\nDatabase Schema:\n{SCHEMA}\n\nRules:\n1. Use exact table and column names\n2. Include table names with columns\n3. Consider relationships for joins\n4. Keep expansion minimal\n5. Don't add unsolicited conditions"},
                {"role": "user", "content": f"Original query: {query}\nReformulate this query:"}
            ],
            temperature=0.1
        )
        return response.choices[0].message.content.strip()

    def create_query_plan(self, expanded_query: str) -> Dict[str, str]:
        """Create structured query plan"""
        response = self.client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": f"Create a JSON query plan with these components:\n{{'tables': 'required tables', 'columns': 'required columns with table names', 'filters': 'WHERE conditions', 'ordering': 'ORDER BY requirements'}}\n\nSchema:\n{SCHEMA}"},
                {"role": "user", "content": expanded_query}
            ],
            temperature=0.1
        )
        
        plan_text = response.choices[0].message.content.strip().replace("```json", "").replace("```", "")
        try:
            import json
            return json.loads(plan_text)
        except:
            try:
                return eval(plan_text)
            except:
                return {
                    "tables": "Parse failed",
                    "columns": "Parse failed",
                    "filters": "Parse failed",
                    "ordering": "Parse failed",
                    "raw_response": plan_text
                }

    def generate_sql_query(self, expanded_query: str, query_plan: Dict[str, str]) -> str:
        """Generate SQL from expanded query and plan"""
        response = self.client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": f"Generate PostgreSQL query based on:\nSchema:\n{SCHEMA}\n\nPlan:\nTables: {query_plan['tables']}\nColumns: {query_plan['columns']}\nFilters: {query_plan['filters']}\nOrdering: {query_plan['ordering']}\n\nReturn only SQL query."},
                {"role": "user", "content": expanded_query}
            ],
            temperature=0.1
        )
        return response.choices[0].message.content.strip()

    def refine_failed_query(self, failed_query: str, sql_error: str, expanded_query: str) -> str:
        """Refine failed query using error context"""
        self.error_context.append({
            "query": failed_query,
            "error": sql_error,
            "expanded": expanded_query
        })
        
        error_history = "\n".join([
            f"Previous attempt {i+1}:\nQuery: {err['query']}\nError: {err['error']}"
            for i, err in enumerate(self.error_context[-3:])  # Last 3 attempts
        ])
        
        response = self.client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": f"Fix failed query using schema:\n{SCHEMA}\n\nError history:\n{error_history}\n\nFocus on:\n1. Correct relationships\n2. Valid columns\n3. Proper syntax\n4. Logical structure"},
                {"role": "user", "content": f"Failed query: {failed_query}\nExpanded: {expanded_query}\nError: {sql_error}\n\nProvide refined query:"}
            ],
            temperature=0.1
        )
        return response.choices[0].message.content.strip()

    def summarize_result(self, question: str, sql_query: str, result: str) -> str:
        """Summarize query results"""
        response = self.client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Provide clear, concise summary of query results focusing on key findings."},
                {"role": "user", "content": f"Question: {question}\nSQL: {sql_query}\nResult: {result}\n\nSummarize:"}
            ],
            temperature=0.7
        )
        return response.choices[0].message.content.strip()

class RAGSystem:
    def __init__(self):
        self.db = Database()
        self.llm = OpenAILLM()
        self.max_retries = 3

    def process_query(self, user_query: str) -> dict:
        result = {
            "user_query": user_query,
            "expanded_query": None,
            "query_plan": None,
            "raw_plan_response": None,
            "sql_query": None,
            "raw_result": None,
            "summary": None,
            "error": None,
            "refinement_attempts": []
        }

        try:
            result["expanded_query"] = self.llm.expand_query(user_query)
            result["query_plan"] = self.llm.create_query_plan(result["expanded_query"])
            result["raw_plan_response"] = result["query_plan"].get('raw_response')
            result["sql_query"] = self.llm.generate_sql_query(result["expanded_query"], result["query_plan"])

            retries = 0
            while retries < self.max_retries:
                try:
                    rows, columns = self.db.execute_query(result["sql_query"])
                    result["raw_result"] = pd.DataFrame(rows, columns=columns)
                    result["summary"] = self.llm.summarize_result(
                        user_query, result["sql_query"], 
                        result["raw_result"].to_string(index=False)
                    )
                    break
                except Exception as sql_error:
                    retries += 1
                    if retries == self.max_retries:
                        raise Exception(f"Max retries reached. Last error: {str(sql_error)}")
                    
                    result["refinement_attempts"].append({
                        "attempt": retries,
                        "failed_query": result["sql_query"],
                        "error": str(sql_error)
                    })
                    
                    result["sql_query"] = self.llm.refine_failed_query(
                        result["sql_query"], str(sql_error), result["expanded_query"]
                    )

        except Exception as e:
            result["error"] = str(e)

        return result

    def close(self):
        self.db.close()

def main():
    st.set_page_config(page_title="RAG System with OpenAI", page_icon="🤖", layout="wide")
    st.title("RAG System with OpenAI")

    if OPENAI_API_KEY == "your-api-key-here":
        st.error("Please set OPENAI_API_KEY")
        return

    rag_system = RAGSystem()

    st.sidebar.header("Database Schema")
    st.sidebar.text(SCHEMA)

    user_query = st.text_input("Enter your question:")

    if st.button("Process Query"):
        if user_query:
            with st.spinner("Processing query..."):
                result = rag_system.process_query(user_query)
            
            if result["error"]:
                st.error(f"Error: {result['error']}")
            
            if result["refinement_attempts"]:
                st.subheader("Query Refinement History")
                for attempt in result["refinement_attempts"]:
                    with st.expander(f"Attempt {attempt['attempt']}"):
                        st.code(attempt["failed_query"], language="sql")
                        st.error(attempt["error"])
            
            if not result["error"]:
                st.subheader("Expanded Query")
                st.write(result["expanded_query"])
                
                st.subheader("Query Plan")
                st.json(result["query_plan"])
                
                if result["raw_plan_response"]:
                    st.text(result["raw_plan_response"])
                
                st.subheader("Final SQL Query")
                st.code(result["sql_query"], language="sql")
                
                st.subheader("Results")
                st.dataframe(result["raw_result"])
                
                st.subheader("Summary")
                st.write(result["summary"])
        else:
            st.warning("Please enter a question.")

    if st.button("Quit"):
        rag_system.close()
        st.stop()

if __name__ == "__main__":
    main()
###