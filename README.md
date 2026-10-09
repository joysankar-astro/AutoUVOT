# AutoUVOT
Automated Swift UVOT Data Reduction Pipeline

AutoUVOT is a small Flask web service that builds Swift/UVOT light curves for a
sky position. You give it an RA and DEC. It then:

1. Queries the HEASARC `swiftmastr` catalog for Swift observations within 0.5° of
   the position that have more than 10 s of UVOT exposure and are already public.
2. Skips any observation ID already stored in the database for that position
   (within 3″).
3. Downloads the UVOT sky images (`*_sk.img.gz`) and exposure maps
   (`*_ex.img.gz`) for each new observation from the public `nasa-heasarc` S3
   bucket.
4. Runs HEASoft `uvotsource` on each image. It uses a 5″ source circle and a
   6″–12″ background annulus, both centred on the position.
5. Writes the photometry (or a 3σ upper limit) to a PostgreSQL table and deletes
   the downloaded files.

The stored points can then be fetched through the API or plotted as a light curve
in the browser.

## Repository layout

| File | Purpose |
|------|---------|
| `app.py` | Flask app: web pages, REST API, background job executor |
| `analysis.py` | Pipeline: HEASARC query, S3 download, `uvotsource` photometry, database inserts |
| `templates/index.html` | Form for submitting a new RA/DEC job |
| `templates/results.html` | Form that plots the stored light curve with Chart.js |

## Requirements

- **Python 3.9+**
- **HEASoft** with the Swift tools (`uvotsource` must be on your `PATH`)
- **Swift UVOT CALDB**, installed and set up (`CALDB` environment variable set).
  `uvotsource` needs it.
- **PostgreSQL** that the server can reach
- Internet access to HEASARC and to AWS S3. The S3 bucket is public, so no AWS
  credentials are needed.

Python packages:

```bash
pip install flask "psycopg[binary]" boto3 numpy astropy astroquery
```

## Setup

### 1. Initialise HEASoft and CALDB

Do this in the same shell you will start the server from, so the background jobs
can find `uvotsource`:

```bash
export HEADAS=/path/to/heasoft/<platform>
source $HEADAS/headas-init.sh
export CALDB=/path/to/caldb
source $CALDB/software/tools/caldbinit.sh

which uvotsource   # should print a path
```

### 2. Create the database table

Both `app.py` and `analysis.py` read from and write to a table named `uvot`.
Create it once:

```sql
CREATE TABLE uvot (
    id             SERIAL PRIMARY KEY,
    ra             DOUBLE PRECISION,   -- deg
    dec            DOUBLE PRECISION,   -- deg
    obs_time       DOUBLE PRECISION,   -- observation start, MJD
    filter         TEXT,               -- UVOT filter (e.g. UVW1, U, V); 'N' if photometry failed
    ab_mag         DOUBLE PRECISION,
    ab_mag_err     DOUBLE PRECISION,   -- -1 means ab_mag is a 3σ upper limit
    mag            DOUBLE PRECISION,   -- Vega magnitude
    mag_err        DOUBLE PRECISION,
    swift_obs_id   TEXT,
    analysis_time  DOUBLE PRECISION,   -- when the row was written, MJD
    job_id         TEXT,
    success        BOOLEAN
);
```

### 3. Configure the connection string

The database URL is hard-coded in **two** places. Change both to point at your
database:

- `app.py`: `DB_URL = "postgresql://user:password@host:port/dbname"`
- `analysis.py`: `DB_URL = "postgresql://user:password@host:port/dbname"`

Other settings in `app.py`:

- `ThreadPoolExecutor(max_workers=4)` sets how many jobs run at the same time.
  Set it to suit your machine.
- `API_KEY` is the key for the optional `x-api-key` check. The
  `@require_api_key` decorator on `/api/start` is commented out by default.
  Uncomment it to require the key.

## Running

Start the server from the repository root, because jobs download data into
`./temp/<obsid>/`:

```bash
cd AutoUVOT
python app.py
```

The server listens on `0.0.0.0:8892`.

### Using the web pages

- **Submit a job:** open `http://localhost:8892/`, enter RA and DEC in decimal
  degrees and click *Submit*. You get a job ID straight away. The analysis runs
  in the background, and its progress is printed in the server's terminal.
- **View the light curve:** open `http://localhost:8892/lightcurve`, enter the
  same RA and DEC, and the stored points are plotted.

A target with many observations can take a while to process. Results appear on
the light-curve page as each observation finishes.

### Using the API

**Start a job**

```bash
curl -X POST http://localhost:8892/api/start \
     -H "Content-Type: application/json" \
     -d '{"ra": 83.6331, "dec": 22.0145}'
```

```json
{"job_id": "3f2a…", "status": "started"}
```

If you enabled `@require_api_key`, add `-H "x-api-key: <your key>"`.

**Get results**

```bash
curl "http://localhost:8892/api/result?ra=83.6331&dec=22.0145"
```

This returns every stored row within 3″ of the position that has
`success = TRUE` and `ab_mag < 99`:

```json
[
  {"swift_obs_id": "00012345001", "obs_time": 58000.12, "ab_mag": 17.3,
   "ab_mag_err": 0.05, "filter": "UVW1"},
  ...
]
```

## How to read the results

For each image, `analysis.py` writes one row according to these rules:

| Case | `ab_mag` / `mag` | `*_err` | `success` |
|------|------------------|---------|-----------|
| Detected (≥ 3σ) and `MAG_ERR < 1` | measured magnitude | measured error | `TRUE` |
| Detected (≥ 3σ) but `MAG_ERR ≥ 1` | measured magnitude | measured error | `FALSE` |
| Not detected (< 3σ) | 3σ upper limit | `-1` | `TRUE` |
| `uvotsource` or file error | `-1` | `-1` | `FALSE`, filter = `N` |

- An error of `-1` means the value is an **upper limit**, not a detection.
- `obs_time` is the start time of the Swift observation, in MJD.
- Observations with a stored row are never processed again for that position.
  This includes failed rows. To redo a target, delete its rows from `uvot`
  first.

## Notes

- Each run downloads into `temp/<obsid>/` and deletes that folder when it
  finishes, even if the run fails.
- Jobs are not tracked after they start. The job ID is saved with each row
  (`job_id` column), but there is no status endpoint. Watch the server log, or
  query `/api/result`.
- `app.run` starts Flask's development server. For a public deployment, run it
  behind a production WSGI server and keep the database credentials out of the
  source code.
