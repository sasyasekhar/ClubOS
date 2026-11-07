from sqlalchemy import text

from app.config.database import engine


def check_connection():
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("SELECT current_database(), current_user")
            )

            database, user = result.fetchone()

            print("Database connection successful!")
            print(f"Database: {database}")
            print(f"User: {user}")

    except Exception as error:
        print("Database connection failed!")
        print(error)


if __name__ == "__main__":
    check_connection()

 