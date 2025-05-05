#!/usr/bin/env python
import os
import asyncio
import logging
import json
import time
from httpx import URL
import snowflake.connector
from dotenv import load_dotenv
import mcp.server.stdio
from mcp.server import Server
from mcp.types import Tool, ServerResult, TextContent
from contextlib import closing
from typing import Optional, Any

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("snowflake_server")

load_dotenv()


class WriteAttemptError(Exception):
    pass


class SnowflakeConnection:
    """
    Snowflake database connection management class
    """

    def __init__(self):
        self.config = {
            "user": os.getenv("SNOWFLAKE_USER"),
            "account": os.getenv("SNOWFLAKE_ACCOUNT"),
            "database": os.getenv("SNOWFLAKE_DATABASE"),
        }
        self.conn: Optional[snowflake.connector.SnowflakeConnection] = None
        logger.info(
            f"Initialized with config: {json.dumps({k:v for k,v in self.config.items()})}"
        )

    def ensure_connection(self) -> snowflake.connector.SnowflakeConnection:
        """
        Ensure database connection is available, create new connection if it doesn't exist or is disconnected
        """
        try:
            if self.conn is None:
                logger.info("Creating new Snowflake connection...")
                self.conn = snowflake.connector.connect(
                    **self.config,
                    client_session_keep_alive=True,
                    network_timeout=15,
                    login_timeout=15,
                    authenticator="externalbrowser",
                )
                self.conn.cursor().execute("ALTER SESSION SET TIMEZONE = 'UTC'")
                logger.info("New connection established and configured")

            try:
                self.conn.cursor().execute("SELECT 1")
            except:
                logger.info("Connection lost, reconnecting...")
                self.conn = None
                return self.ensure_connection()

            return self.conn
        except Exception as e:
            logger.error(f"Connection error: {str(e)}")
            raise

    def execute_query(self, query: str) -> list[dict[str, Any]]:
        """
        Execute SQL query and return results

        Args:
            query (str): SQL query statement

        Returns:
            list[dict[str, Any]]: List of query results
        """
        start_time = time.time()
        logger.info(
            f"Executing query: {query[:200]}..."
        )  # Log only first 200 characters

        try:
            conn = self.ensure_connection()
            with conn.cursor() as cursor:
                # Use transaction for write operations
                if any(
                    query.strip().upper().startswith(word)
                    for word in [
                        "INSERT",
                        "UPDATE",
                        "DELETE",
                        "CREATE",
                        "DROP",
                        "ALTER",
                    ]
                ):
                    raise WriteAttemptError(
                        "snowflake MCP currently does not support write operations"
                    )
                else:
                    # Read operations
                    cursor.execute(query)
                    if cursor.description:
                        columns = [col[0] for col in cursor.description]
                        rows = cursor.fetchall()
                        results = [dict(zip(columns, row)) for row in rows]
                        logger.info(
                            f"Read query returned {len(results)} rows in {time.time() - start_time:.2f}s"
                        )
                        return results
                    return []

        except snowflake.connector.errors.ProgrammingError as e:
            logger.error(f"SQL Error: {str(e)}")
            logger.error(f"Error Code: {getattr(e, 'errno', 'unknown')}")
            raise
        except Exception as e:
            logger.error(f"Query error: {str(e)}")
            logger.error(f"Error type: {type(e).__name__}")
            raise

    def close(self):
        """
        Close database connection
        """
        if self.conn:
            try:
                self.conn.close()
                logger.info("Connection closed")
            except Exception as e:
                logger.error(f"Error closing connection: {str(e)}")
            finally:
                self.conn = None


class SnowflakeServer(Server):
    def __init__(self):
        super().__init__(name="snowflake-server")
        self.db = SnowflakeConnection()
        logger.info("SnowflakeServer initialized")

        @self.list_tools()
        async def handle_tools():
            return [
                Tool(
                    name="execute_query",
                    description="Execute a SQL query on Snowflake",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "SQL query to execute",
                            }
                        },
                        "required": ["query"],
                    },
                )
            ]

        @self.call_tool()
        async def handle_call_tool(name: str, arguments: dict):
            if name == "execute_query":
                start_time = time.time()
                try:
                    result = self.db.execute_query(arguments["query"])
                    execution_time = time.time() - start_time

                    return [
                        TextContent(
                            type="text",
                            text=f"Results (execution time: {execution_time:.2f}s):\n{result}",
                        )
                    ]
                except Exception as e:
                    error_message = f"Error executing query: {str(e)}"
                    logger.error(error_message)
                    return [TextContent(type="text", text=error_message)]

    def __del__(self):
        if hasattr(self, "db"):
            self.db.close()


async def main():
    """
    Main function, starts server and handles requests
    """
    try:
        server = SnowflakeServer()
        initialization_options = server.create_initialization_options()
        logger.info("Starting server")

        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, initialization_options)
    except Exception as e:
        logger.critical(f"Server failed: {str(e)}", exc_info=True)
        raise
    finally:
        logger.info("Server shutting down")


if __name__ == "__main__":
    asyncio.run(main())
