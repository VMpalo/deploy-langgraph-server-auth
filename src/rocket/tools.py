"""
Tools for the agent to use.
"""
from langchain_core.tools import tool
from sqlalchemy import create_engine, text, Engine
import pandas as pd
from scout import env
from langgraph.types import Command
from langchain_core.tools.base import InjectedToolCallId
from typing import Annotated
from langchain_core.messages import ToolMessage


class ServerSession:
    """A session for server-side state management and operations. 
    
    In practice, this would be a separate service from where the agent is running and the agent would communicate with it using a REST API. In this simplified example, we use it to persist the db engine and data returned from the query_db tool.
    """
    def __init__(self):
        self.engine: Engine = None
        self.df: pd.DataFrame = None

        self.engine = self._get_engine()

    def _get_engine(self):
        if self.engine is None:
            # Configure SQLAlchemy for session pooling
            _engine = create_engine(
                env.SUPABASE_URL,
                pool_size=5,                # Smaller pool size since the pooler manages connections
                max_overflow=5,             # Fewer overflow connections needed
                pool_timeout=10,            # Shorter timeout for getting connections
                pool_recycle=1800,          # Recycle connections more frequently
                pool_pre_ping=True,         # Keep this to verify connections
                pool_use_lifo=True,         # Keep LIFO to reduce number of open connections
                connect_args={
                    "application_name": "onlyvans_agent",
                    "options": "-c statement_timeout=30000",
                    # Keepalives less important with transaction pooler but still good practice
                    "keepalives": 1,
                    "keepalives_idle": 60,
                    "keepalives_interval": 30,
                    "keepalives_count": 3
                }
            )
            return _engine
        return self.engine


# Create a global instance of the ServerSession
session = ServerSession()


@tool
def query_db(query: str) -> str:
    """Query the database using Postgres SQL.

    Args:
        query: The SQL query to execute. Must be a valid postgres SQL string that can be executed directly.

    Returns:
        str: The query result as a markdown table.
    """
    try:
        # Use the global engine in the server session to connect to Supabase
        with session.engine.connect().execution_options(
            isolation_level="READ COMMITTED"
        ) as conn:
            result = conn.execute(text(query))

            columns = list(result.keys())
            rows = result.fetchall()
            df = pd.DataFrame(rows, columns=columns)

            # Store the DataFrame in the server session
            session.df = df

            conn.close()  # Explicitly close the connection
        return df.to_markdown(index=False)
    except Exception as e:
        return f"Error executing query: {str(e)}"


@tool
def generate_visualization(
    name: str, 
    sql_query: str, 
    plotly_code: str,
    tool_call_id: Annotated[str, InjectedToolCallId]
    ) -> str:
    '''Generate a visualization using Python, SQL, and Plotly. If the visualizaton is successfully generated, it's automatically rendered for the user on the frontend.

    Args:
        name: The name of the visualization. Should be a short name with underscores and no spaces.
        sql_query: The SQL query to retrieve data for the visualization. Must be a valid postgres SQL string that can be executed directly. The query will be executed and the result will be loaded into a DataFrame named 'df'.
        plotly_code: Python code that generates a Plotly figure. The code should create a variable named 'fig' that contains the Plotly figure object.

    Returns:
        str: Success message if successful or an error message.

    ## Assumptions
    Assume the data is already loaded into a DataFrame named 'df' and the following libraries are already imported for immediate use: 
    
    import pandas as pd
    import plotly.express as px
    import plotly.graph_objects as go
    import plotly

    ## Example:
    User asks "Show me the top 5 creators by revenue"

    sql_query = "SELECT c.id, c.first_name, c.last_name, SUM(t.amount_usd) AS total_revenue\nFROM creators c\nJOIN transactions t ON c.id = t.creator_id\nGROUP BY c.id, c.first_name, c.last_name\nORDER BY total_revenue DESC\nLIMIT 5;"
    plotly_code = "fig = px.bar(df, x='first_name', y='total_revenue', title='Top 5 Creators by Revenue')\nfig.update_layout(xaxis_title='Creator', yaxis_title='Total Revenue ($)')"
    '''
    import io
    import os
    from contextlib import redirect_stdout, redirect_stderr

    # Create the output directory if it doesn't exist
    os.makedirs("output", exist_ok=True)

    # Set the output file path
    file_path = f"output/{name}.json"

    # Capture stdout and stderr
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    # Add SQL query to the code

    pre_code = f'''
from sqlalchemy import text
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import plotly

# Generated SQL
df = pd.read_sql(text("""{sql_query}"""), engine)

# Generated plotly code
'''
    post_code = f'''

# Save the figure to JSON
if 'fig' in locals() or 'fig' in globals():
    fig_json = pio.to_json(fig)
    with open('{file_path}', 'w') as f:
        f.write(fig_json)
'''
    
    # Sandwich the plotly code like this to avoid indent errors from f-string
    code = pre_code + plotly_code + post_code

    # Prepare execution environment with database connection
    exec_globals = {}

    # Pass the server session engine to the code
    if "engine" in code:
        exec_globals['engine'] = session.engine

    try:
        # Execute the code with captured output
        print(f"Executing code: \n\n{code}\n\n")
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(code, exec_globals, {})

        # Get the output and error messages
        print(f"STDOUT: \n\n{stdout_capture.getvalue()}\n")
        print(f"STDERR: \n\n{stderr_capture.getvalue()}\n")

        # Check if the fig was created
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                fig_json = f.read()
            return Command(
                update={
                    # update the state keys
                    "chart_json": fig_json,
                    # update the message history
                    "messages": [
                        ToolMessage(
                            "Visualization created successfully.", 
                            tool_call_id=tool_call_id
                        )
                    ],
                }
            )
        else:
            raise Exception(f"Error: Failed to generate visualization.\n\n<stderr>\n{stderr_capture.getvalue()}\n</stderr>")

    except Exception as e:
        # Get the error message
        error_message = str(e)
        return f"Error executing visualization code: {error_message}"
