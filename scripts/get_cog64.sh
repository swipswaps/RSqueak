#!/bin/bash

if [ -n "$1" ]; then
    echo "Downloading version ${1}"
    COGURL="http://www.mirandabanda.org/files/Cog/VM/VM.r$1/"
else
    echo "Downloading latest Cog VM"
    COGURL="http://www.mirandabanda.org/files/Cog/VM/latest/"
fi

string="$(uname -m)"
if [[ "$string" == *"arm"* ]]; then
    exit 0
else
    zip=$(curl -s $COGURL | grep -o "href=\"cogspur64linuxht-.*tgz\"" | tail -1)
fi

zip=${zip#*=}
zip=${zip#\"}
zip=${zip%%\"}
curl -O "$COGURL/$zip"
rm -rf cog64
tar xzf $zip
rm $zip
mv cogspurlinux* tmpcog
mv tmpcog cog64
