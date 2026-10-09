import glob
import os
import shutil
import subprocess
from datetime import datetime
from glob import glob

import boto3
import numpy as np
import psycopg
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.time import Time
from astropy.wcs import WCS
from astroquery.heasarc import Heasarc
from botocore import UNSIGNED
from botocore.client import Config


# your PostgreSQL database connection string
DB_URL = "postgresql://xyz:abc_psql183@localhost:1122/xyzdb"


def collect_swift_pairs(folder):
    folder = os.path.abspath(folder)

    sk_files = glob(os.path.join(folder, "*_sk.img.gz"))
    ex_files = glob(os.path.join(folder, "*_ex.img.gz"))

    def get_base(path):
        filename = os.path.basename(path)
        return filename.replace("_sk.img.gz", "").replace("_ex.img.gz", "")

    sk_map = {get_base(f): f for f in sk_files}
    ex_map = {get_base(f): f for f in ex_files}

    sk_list = []
    ex_list = []
    base_list = []

    for base in sorted(sk_map.keys()):
        if base in ex_map:
            sk_list.append(os.path.abspath(sk_map[base]))
            ex_list.append(os.path.abspath(ex_map[base]))
            base_list.append(base)

    return sk_list, ex_list, base_list


def insert_uvot_row(
    ra,
    dec,
    obs_time,
    filter_name,
    ab_mag,
    ab_mag_err,
    mag,
    mag_err,
    swift_obs_id,
    analysis_time,
    job_id,
    success_bool,
):
    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO uvot
                (ra, dec, obs_time, filter, ab_mag, ab_mag_err,
                 mag, mag_err, swift_obs_id, analysis_time,job_id,success)
                VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    ra,
                    dec,
                    obs_time,
                    filter_name,
                    ab_mag,
                    ab_mag_err,
                    mag,
                    mag_err,
                    swift_obs_id,
                    analysis_time,
                    job_id,
                    success_bool,
                ),
            )
            conn.commit()


def find_obsids_within_3arcsec(ra_deg, dec_deg):
    query = f"""
    SELECT DISTINCT swift_obs_id
    FROM uvot
    WHERE DEGREES(SQRT(
               POW(RADIANS(ra - {ra_deg}) * COS(RADIANS(dec)), 2) +
               POW(RADIANS(dec - {dec_deg}), 2)
           )) * 3600 <= 3;
    """

    with psycopg.connect(DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            obsids = [row[0] for row in cur.fetchall()]

    return obsids


def run_analysis(job_id, ra, dec):
    print(f"Running analysis for RA={ra}, DEC={dec}")

    pos = SkyCoord(f"{ra} {dec}", unit=u.deg)
    xamin_tab = Heasarc.query_region(pos, catalog="swiftmastr", radius=0.5 * u.deg)
    xamin_data = xamin_tab.to_pandas()
    xamin_data = xamin_data[xamin_data["uvot_exposure"] > 10]
    now = datetime.utcnow()
    mjd_now = Time(now, scale="utc").mjd
    xamin_data = xamin_data[xamin_data["archive_date"] < mjd_now]

    print(f"Found {len(xamin_data)} archived observations...")

    existing_obsids = find_obsids_within_3arcsec(ra, dec)
    remote_obsids = set(xamin_data["obsid"].astype(str))
    new_obsids = remote_obsids - set(existing_obsids)

    if new_obsids:
        print(f"Found {len(new_obsids)} observations to be analysised...")
        for obsid in new_obsids:
            obs_path = f"temp/{obsid}/"
            start_mjd = xamin_data.loc[
                xamin_data["obsid"] == obsid, "start_time"
            ].values[0]
            obs_date_code = (
                Time(start_mjd, format="mjd").to_datetime().strftime("%Y_%m")
            )

            if os.path.exists(obs_path) and os.path.isdir(obs_path):
                print(f"{obsid} already exist")
            else:
                print(f"Downloading {obsid}")
                s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
                bucket, prefix, outdir = (
                    "nasa-heasarc",
                    f"swift/data/obs/{obs_date_code}/{obsid}/uvot/image/",
                    f"temp/{obsid}/",
                )
                os.makedirs(outdir, exist_ok=True)

                for obj in s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get(
                    "Contents", []
                ):
                    key = obj["Key"]
                    if key.endswith("/"):
                        continue
                    local_path = os.path.join(outdir, key[len(prefix) :])
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    s3.download_file(bucket, key, local_path)

            img_path = f"temp/{obsid}/"

            try:
                sk_list, ex_list, base_list = collect_swift_pairs(img_path)

                for ino in range(len(base_list)):
                    print("------------------------------------------------")
                    print(f"Working for base file: {base_list[ino]}")
                    print("------------------------------------------------")

                    try:
                        bkg_content = f"""fk5;annulus({ra},{dec},6.0",12.0")"""
                        src_content = f"""fk5;circle({ra},{dec},5.0")"""

                        bkg_file_path = os.path.join(img_path, "bkg.reg")
                        src_file_path = os.path.join(img_path, "src.reg")

                        with open(bkg_file_path, "w") as f:
                            f.write(bkg_content)
                        with open(src_file_path, "w") as f:
                            f.write(src_content)

                        output_file = os.path.join(
                            img_path, f"{base_list[ino]}_out.fits"
                        )
                        spath = os.path.abspath(src_file_path)
                        bpath = os.path.abspath(bkg_file_path)
                        opath = os.path.abspath(output_file)

                        cmd_xrt = f"uvotsource image={sk_list[ino]} srcreg={spath} bkgreg={bpath} sigma=3.0 outfile={opath} chatter=0 clobber=YES"
                        subprocess.run(cmd_xrt, shell=True, check=True, close_fds=True)

                        with fits.open(opath) as hdul_run:
                            data_run = hdul_run[1].data.copy()
                        AB_MAG = data_run["AB_MAG"]
                        AB_MAG_ERR = data_run["AB_MAG_ERR"]
                        MAG = data_run["MAG"]
                        MAG_ERR = data_run["MAG_ERR"]
                        FILTER_SWIFT = data_run["FILTER"]
                        DETECTION_SIGMA = data_run["NSIGMA"]
                        MAG_LIM = data_run["MAG_LIM"]
                        AB_MAG_LIM = data_run["AB_MAG_LIM"]

                        now2 = datetime.utcnow()
                        mjd_now2 = Time(now2, scale="utc").mjd

                        if DETECTION_SIGMA[0] >= 3.0:
                            if MAG_ERR[0] < 1.0:
                                insert_uvot_row(
                                    ra,
                                    dec,
                                    start_mjd,
                                    FILTER_SWIFT[0],
                                    AB_MAG[0],
                                    AB_MAG_ERR[0],
                                    MAG[0],
                                    MAG_ERR[0],
                                    obsid,
                                    mjd_now2,
                                    job_id,
                                    True,
                                )
                            else:
                                insert_uvot_row(
                                    ra,
                                    dec,
                                    start_mjd,
                                    FILTER_SWIFT[0],
                                    AB_MAG[0],
                                    AB_MAG_ERR[0],
                                    MAG[0],
                                    MAG_ERR[0],
                                    obsid,
                                    mjd_now2,
                                    job_id,
                                    False,
                                )
                        else:
                            insert_uvot_row(
                                ra,
                                dec,
                                start_mjd,
                                FILTER_SWIFT[0],
                                AB_MAG_LIM[0],
                                -1.0,
                                MAG_LIM[0],
                                -1.0,
                                obsid,
                                mjd_now2,
                                job_id,
                                True,
                            )

                        del data_run

                    except Exception as e:
                        print("Error:", e)
                        now2 = datetime.utcnow()
                        mjd_now2 = Time(now2, scale="utc").mjd
                        insert_uvot_row(
                            ra,
                            dec,
                            start_mjd,
                            "N",
                            -1,
                            -1.0,
                            -1,
                            -1.0,
                            obsid,
                            mjd_now2,
                            job_id,
                            False,
                        )
                        continue

                    print("------------------------------------------------")
                    print(f"Done for base file: {base_list[ino]}")
                    print("------------------------------------------------")
            finally:
                if os.path.exists(obs_path):
                    shutil.rmtree(obs_path)

    else:
        print("No new obs_id to perform the task....")

    print(f"Done with job: {job_id}")
