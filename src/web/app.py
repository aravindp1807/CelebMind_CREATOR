import os
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

def create_app() -> Flask:
    """Create and configure the Flask web application."""
    app = Flask(__name__)
    
    # Configuration
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-12345")
    app.config["DATABASE_URL"] = os.getenv("DATABASE_URL")
    
    # Configure Flask for file uploads
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB
    app.config["UPLOAD_FOLDER"] = os.path.join("output", "uploads")
    
    # Ensure output directories exist on startup
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(os.path.join("output", "scraped_raw"), exist_ok=True)
    os.makedirs(os.path.join("output", "vault"), exist_ok=True)
    
    # Register blueprints
    from src.web.routes import bp as web_bp
    app.register_blueprint(web_bp)
    
    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, host="0.0.0.0", port=5000)
