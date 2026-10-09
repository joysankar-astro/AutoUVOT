import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
from flask import Flask, jsonify, render_template, request

from analysis import run_analysis

app = Flask(__name__)

# your PostgreSQL database connection string
DB_URL = "postgresql://xyz:abc_psql183@localhost:1122/xyzdb"

# Change the number of workers based on your server's capacity and expected load
executor = ThreadPoolExecutor(max_workers=4)

# Replace with your actual API key if you want to use API key authentication
API_KEY = "TEST_API_KEY_123"


def find_points_within_3arcsec(ra_deg, dec_deg):
    query = f"""
    SELECT swift_obs_id, obs_time, ab_mag, ab_mag_err,filter
    FROM uvot
    WHERE ab_mag < 99
        AND success = TRUE
        AND DEGREES(SQRT(
               POW(RADIANS(ra - {ra_deg}) * COS(RADIANS(dec)), 2) +
               POW(RADIANS(dec - {dec_deg}), 2)
           )) * 3600 <= 3;
    """

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            results = cur.fetchall()

    sources = []
    for row in results:
        sources.append(
            {
                "swift_obs_id": row[0],
                "obs_time": row[1],
                "ab_mag": row[2],
                "ab_mag_err": row[3],
                "filter": row[4],
            }
        )

    return sources


def require_api_key(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("x-api-key")
        if key != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/lightcurve")
def lc_result():
    return render_template("results.html")


@app.route("/api/start", methods=["POST"])
# @require_api_key
def start_job():
    data = request.get_json()
    ra = data.get("ra")
    dec = data.get("dec")

    if ra is None or dec is None:
        return jsonify({"error": "Missing RA or DEC"}), 400

    job_id = str(uuid.uuid4())

    executor.submit(run_job, job_id, ra, dec)
    return jsonify({"job_id": job_id, "status": "started"})


@app.get("/api/result")
def get_data():
    ra = request.args.get("ra", type=float)
    dec = request.args.get("dec", type=float)

    if ra is None or dec is None:
        return jsonify({"error": "RA and DEC are required"}), 400

    data_points = find_points_within_3arcsec(ra, dec)
    return jsonify(data_points)


def run_job(job_id, ra, dec):
    with app.app_context():
        try:
            run_analysis(job_id, ra, dec)
        except Exception as e:
            print("Error:", e)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8892)
