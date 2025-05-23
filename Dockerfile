# Use official Python image
FROM python:3.9-slim

# Set the working directory
WORKDIR /app

# Copy requirements.txt and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose port 5000 for Flask
EXPOSE 5000

# Set the command to run the Flask app
CMD ["python", "-u", "-m","gunicorn", "--workers", "1", "--threads=2", "--timeout", "1800", "--bind", "0.0.0.0:5000", "app:app"]