#!/bin/sh
python3 -m pip install pyinstaller
pyinstaller --noconfirm --windowed --name TenzXcraft tenzxcraft.py
