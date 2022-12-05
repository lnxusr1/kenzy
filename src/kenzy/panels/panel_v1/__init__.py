'''
Kenzy.Ai: Synthetic Human
Created on July 12, 2020
@author: lnxusr1
@license: MIT License
@summary: Core Library
'''

# Imports for built-in features
from .panel import PanelApp as RaspiPanel

# version as tuple for simple comparisons 
from kenzy import VERSION, __version__
__appyear__ = "2022"

# string created from tuple to avoid inconsistency 
__app_name__ = "Kenzy.Ai: Control Panel Device Plugin"