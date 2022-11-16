import os
import logging


def _downloadFile(url, folderName, overwrite=False):
    import requests

    local_filename = url.split('/')[-1]
    myFileName = os.path.join(os.path.expanduser("~/.kenzy"), 'data', 'models', folderName, local_filename)
    os.makedirs(os.path.dirname(myFileName), exist_ok=True)
    
    if os.path.isfile(myFileName) and not overwrite:
        print("File exists.  Skipping.")
        return True  # File already exists
    
    if os.path.isfile(myFileName) and overwrite:
        os.remove(myFileName)
        
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(myFileName + ".tmp", 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192): 
                f.write(chunk)
        
        os.rename(myFileName + ".tmp", myFileName)
        
    print("Download successful.")        
    return True


def _getImport(libs, val):
    """
    Dynamically imports a library into memory for referencing during configuration file parsing.
    
    Args:
        libs (list):  The list of libraries already imported.
        val (str):  The name of the new library to import.
    
    Returns:
        (str): The name of the newly imported library.  If library is already imported then returns None.
    """

    if val is not None and isinstance(val, str) and val.strip() != "":
        if "." in val:
            parts = val.split(".")
            parts.pop()
            ret = ".".join(parts)
            if ret in libs:
                return None

            libs.append(ret)
            return ret

    return None


def download_models(model_type="tflite", version=None, include_scorer=False, overwrite=False):
    if version is None:
        version = "1.0.0"
        
    if str(model_type).lower() in ["pbmm", "tflite"]:
        
        model_url = "https://coqui.gateway.scarf.sh/english/coqui/v" + version + "-huge-vocab/model." + str(model_type).lower()
        scorer_url = "https://coqui.gateway.scarf.sh/english/coqui/v" + version + "-huge-vocab/huge-vocabulary.scorer"
        
        print("Downloading", model_url)
        ret = _downloadFile(model_url, "speech", overwrite=overwrite)
        
        if ret and include_scorer:
            print("Downloading", scorer_url)
            ret = _downloadFile(scorer_url, "speech", overwrite=overwrite)
        
        if not ret:
            print("An error occurred downloading the models.")
    
        return ret 
    
    else:
        logging.error("Model type (" + str(model_type) + ") not expected.")
        return False