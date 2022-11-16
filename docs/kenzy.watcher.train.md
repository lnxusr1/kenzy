# Training your Watcher

Kenzy has built-in capacity for face detection.  This is done using the [haarcascade classifiers](https://www.geeksforgeeks.org/python-haar-cascades-for-object-detection/) in conjunction with openCV.

All three haarcascade classifiers are included in the installation package and available under "kenzy_watcher/data/models/watcher".  The haarcascade_frontal_default.xml is used if no classifier is explicitly specified.

To train your model the train() method needs to be called.  The most simple method is:

```
python3 -m kenzy --training-source-folder /path/to/faces-directory
```
You can force the model to be retrained by adding the ```--force-train``` option.

Your faces directory should be configured as follows:
```
/faces-directory
   - /Jane
       - /image1.jpg
       - /image2.jpg
       - /image3.jpg
   - /John
       - /image1.jpg
       - /image2.jpg
       - /image3.jpg
```

This will create a ```recognizer.yml``` file and a ```names.json``` file.  These files are both used to determine who Kenzy sees when capturing video.  If you already have a recognizer and names file built you can specify them with the ```recognizerFile``` and ```namesFile``` parameters when creating a new Watcher device.  View the file ```~/.kenzy/config.json``` to configure specific runtime options.

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)