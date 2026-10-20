import json


def write_jsonl(records, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="\n") as file:
        for record in records:
            line = json.dumps(record, ensure_ascii=False)
            file.write(line + "\n")