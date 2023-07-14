import os
import yaml
import json


def load(file_name=None):
    if file_name is not None and not os.path.isfile(file_name):
        raise FileNotFoundError("Could not find configuration file.")

    if file_name is None:
        file_name = os.path.join(os.path.expanduser("~"), ".kenzy", "config.yml")

    if os.path.isfile(file_name):
        if file_name.lower().endswith(".yml") or file_name.lower().endswith(".yaml"):
            with open(file_name, "r", encoding="UTF-8") as fp:
                return yaml.safe_load(fp)
            
        elif file_name.lower().endswith(".jsn") or file_name.lower().endswith(".json"):
            with open(file_name, "r", encoding="UTF-8") as fp:
                return json.load(fp)
        
        else:
            raise NotImplementedError("Unexpected file extension.")
    else:
        return {}


def save(data, file_name=None):
    if file_name is None:
        file_name = os.path.join(os.path.expanduser("~"), ".kenzy", "config.yml")
    
    dirName = os.path.dirname(file_name)
    if dirName.replace(".", "").replace("/", "").replace("\\", "").strip() != "":
        os.makedirs(dirName, exist_ok=True)

    if file_name.lower().endswith(".yml") or file_name.lower().endswith(".yaml"):
        with open(file_name, "w", encoding="UTF-8") as fp:
            yaml.safe_dump(data, fp)

    elif file_name.lower().endswith(".jsn") or file_name.lower().endswith(".json"):
        with open(file_name, "w", encoding="UTF-8") as fp:
            json.dump(data, fp, indent=4)

    else:
        raise NotImplementedError("Unexpected file extension.")