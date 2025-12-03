# SymNote 🧠

## Project Overview

SymNote is a Streamlit web application that acts as an AI-powered note-taking and task management tool. It allows users to input notes, which are then automatically categorized by an AI into tasks, ideas, or "someday" items. The app provides different views for managing these items, including an inbox, a task organizer, a "today" view with AI-powered task suggestions, a "week" view with a weekly review, and a calendar view. The backend uses a SQLite database to store the data.

## Building and Running

### Local Development

1.  **Set up the environment:**
    ```bash
    python -m venv .venv
    # On Windows
    .venv\Scripts\activate
    # On macOS/Linux
    source .venv/bin/activate
    ```
2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
    
3.  **Configure environment variables:**
    ```bash
    cp .env.example .env
    ```
    Then, edit the `.env` file to add your `OPENAI_API_KEY` and other necessary configurations.

4.  **Run the application:**
    ```bash
    streamlit run src/symnote/app.py
    ```

### Docker

1.  **Configure environment variables:**
    ```bash
    cp .env.example .env
    ```
    Make sure to adjust `DB_PATH` in the `.env` file for the Docker environment (e.g., `/app/symnote.db`).

2.  **Build and run with Docker Compose:**
    ```bash
    docker compose up --build
    ```
    The application will be available at `http://localhost:8501`.

## Development Conventions

*   **Project Structure:** The main application code is located in `src/symnote`, with core logic separated into the `src/symnote/core` directory.
*   **Database:** The application uses SQLite, and the schema is defined in `src/symnote/core/db.py`.
*   **AI Features:** The core AI logic for text classification and task suggestion is in `src/symnote/core/nlp.py`. The weekly review generation is in `src/symnote/core/weekly_review.py`.
*   **Dependencies:** Project dependencies are managed in the `requirements.txt` file.
*   **Testing:** The project has a `tests` directory, but it is not yet fully implemented.
*   **Coding Style:** The code uses Python 3.12, with type hints and a functional approach in many places.

## Key Files

*   `src/symnote/app.py`: The main Streamlit application file that defines the UI and ties together the different components.
*   `src/symnote/calendar_app.py`: The calendar view component.
*   `src/symnote/core/db.py`: Handles all database interactions, including schema initialization and CRUD operations.
*   `src/symnote/core/nlp.py`: Contains the logic for the AI features, such as text classification and task prioritization.
*   `src/symnote/core/weekly_review.py`: Generates the weekly review report.
*   `requirements.txt`: Lists all the Python dependencies for the project.
*   `README.md`: Provides a good overview of the project, including setup instructions.
*   `docker-compose.yml` and `docker/Dockerfile`: Define the Docker configuration for the application.
