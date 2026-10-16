FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Built by scripts/build_space.py: app.py plus learning_coach/, agents/ and prompts.
COPY . .

# Hugging Face routes the Space to app_port 7860 (see README.md front matter).
ENV PORT=7860 \
    LEARNING_COACH_DATA_DIR=/tmp/learning-coach

EXPOSE 7860
CMD ["python", "app.py"]
