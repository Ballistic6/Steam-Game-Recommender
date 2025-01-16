import argparse
import csv
import json
import time
from datetime import datetime

import requests
import mysql.connector
from fastapi import FastAPI

app = FastAPI()

# ---- MySQL CONNECTION ----
conn = mysql.connector.connect(
    host="localhost",
    user="root",
    passwd="testpassword1",
    database="mydb"
)
cursor = conn.cursor()


def gather_all_ids(API_KEY):
    BASE_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
    
    params = {
        "key": API_KEY,
        "include_games": True,
        "include_dlc": False,
        "include_software": False,
        "include_videos": False,
        "include_hardware": False,
        "max_results": 50000, 
    }

    all_apps = []
    last_appid = 0

    while True:
        params["last_appid"] = last_appid

        response = requests.get(BASE_URL, params=params)

        if response.status_code == 200:
            data = response.json()
            apps = data.get("response", {}).get("apps", [])

            if not apps:
                break 

            all_apps.extend(apps)
            last_appid = apps[-1]["appid"]  

            print(f"Fetched {len(apps)} apps, total so far: {len(all_apps)}")
        else:
            print(f"Failed to fetch data. Status code: {response.status_code}")
            break

    unique_apps = {app["appid"]: app for app in all_apps}.values()

    csv_file = "all_steam_game_ids.csv"

    with open(csv_file, mode="w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["app_id", "name"])

        for app in unique_apps:
            writer.writerow([app.get("appid"), app.get("name")])

    print(f"Data successfully saved to {csv_file}. Total unique apps: {len(unique_apps)}")



def store_app_details_in_db():
    """
    Reads 'all_steam_game_ids.csv', fetches details for each app_id,
    and upserts into 'steam_app_details'. Also normalizes categories
    and genres into 'steam_app_categories' & 'steam_app_genres'.
    """
    batch_counter = 0
    csv_file = "all_steam_game_ids.csv"
    try:
        with open(csv_file, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)  
    except FileNotFoundError:
        print(f"File '{csv_file}' not found. Please run 'gather_all_ids' first.")
        return

    for i, row in enumerate(rows, start=1):
        app_id = row["app_id"]
        if not app_id.isdigit():
            continue 

        details_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}"
        try:
            response = requests.get(details_url, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f"Request error for app_id={app_id}: {e}")
            continue

        if response.status_code != 200:
            print(f"Failed to fetch data for app_id={app_id}. HTTP {response.status_code}")
            continue

        try:
            data = response.json()  
        except ValueError:
            print(f"Invalid JSON response for app_id={app_id}")
            continue

        app_key = str(app_id)
        app_info = data.get(app_key, {})
        if not app_info.get("success", False):
            continue

        details = app_info.get("data", {})

        # Extract fields
        name = details.get("name", "")
        is_free = 1 if details.get("is_free", False) else 0

        # Extract release_date info
        release_date_info = details.get("release_date", {})
        coming_soon = 1 if release_date_info.get("coming_soon", False) else 0
        
        # Parse the "Jul 9, 2013" style date
        raw_date_str = release_date_info.get("date", "")
        release_date_date = None
        if raw_date_str:
            try:
                release_date_date = datetime.strptime(raw_date_str, "%b %d, %Y").date()
            except ValueError:
                pass

        rec_data = details.get("recommendations", {})
        recommendations_count = rec_data.get("total", 0)

        raw_data_json = json.dumps(details)

        upsert_sql = """
        INSERT INTO steam_app_details (
            app_id,
            name,
            coming_soon,
            release_date_date,
            is_free,
            recommendations,
            raw_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            name = VALUES(name),
            coming_soon = VALUES(coming_soon),
            release_date_date = VALUES(release_date_date),
            is_free = VALUES(is_free),
            recommendations = VALUES(recommendations),
            raw_json = VALUES(raw_json),
            fetched_at = CURRENT_TIMESTAMP
        """
        vals = (
            app_id,
            name,
            coming_soon,
            release_date_date,  
            is_free,
            recommendations_count,
            raw_data_json
        )

        try:
            cursor.execute(upsert_sql, vals)
        except mysql.connector.Error as err:
            print(f"MySQL error for app_id={app_id}: {err}")
            continue

        # Delete old categories and genres for simplicity
        delete_cats_sql = "DELETE FROM steam_app_categories WHERE app_id=%s"
        delete_gens_sql = "DELETE FROM steam_app_genres WHERE app_id=%s"
        cursor.execute(delete_cats_sql, (app_id,))
        cursor.execute(delete_gens_sql, (app_id,))

        # Insert categories
        categories = details.get("categories", [])
        for cat_obj in categories:
            cat_name = cat_obj.get("description", "")
            if cat_name:
                cat_insert_sql = """
                INSERT INTO steam_app_categories (app_id, category_name)
                VALUES (%s, %s)
                """
                cursor.execute(cat_insert_sql, (app_id, cat_name))

        # Insert genres
        genres = details.get("genres", [])
        for gen_obj in genres:
            gen_name = gen_obj.get("description", "")
            if gen_name:
                gen_insert_sql = """
                INSERT INTO steam_app_genres (app_id, genre_name)
                VALUES (%s, %s)
                """
                cursor.execute(gen_insert_sql, (app_id, gen_name))

        batch_counter += 1
        if batch_counter == 1000:
            conn.commit()
            batch_counter = 0
            time.sleep(1.5)

        # Print progress every 100 games
        if i % 100 == 0:
            print(f"Processed {i} apps so far...")

    if batch_counter > 0:
        conn.commit()

    print("Finished storing all app details into the database.")

def main():
    parser = argparse.ArgumentParser(description="Gather Steam Store data.")
    parser.add_argument("--type", choices=["all-ids", "store-details"], help="Type of data to gather/store")
    args = parser.parse_args()

    API_KEY = ""
    try:
        with open('./environment.txt', "r") as file:
            API_KEY = file.read().strip()
    except FileNotFoundError:
        print("Environment file not found at 'environment.txt'.")

    if args.type == "all-ids":
        gather_all_ids(API_KEY)
    elif args.type == "store-details":
        store_app_details_in_db()
    else:
        print("Please use --type all-ids or --type store-details")


if __name__ == "__main__":
    main()
