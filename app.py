from flask import Flask, render_template
from services.db_service import init_db

app = Flask(__name__)
app.config["SECRET_KEY"] = "neu-smart-parking-secret-key"


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True)