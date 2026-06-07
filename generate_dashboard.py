from notion_client import Client
import os

notion = Client(auth=os.environ["NOTION_TOKEN"])

DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

response = notion.databases.query(
    database_id=DATABASE_ID
)

tasks = []

for page in response["results"]:

    props = page["properties"]

    try:
        title = props["Name"]["title"][0]["plain_text"]
    except:
        continue

    try:
        status = props["Status"]["select"]["name"]
    except:
        status = ""

    if status.lower() != "done":
        tasks.append(title)

with open("dashboard.md", "w", encoding="utf-8") as f:

    f.write("# Kindle Dashboard\n\n")

    f.write("## Tasks\n\n")

    for task in tasks:
        f.write(f"- [ ] {task}\n")
