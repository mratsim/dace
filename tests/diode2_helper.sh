#!/bin/bash

# File helper that creates a diode2 server for test tasks to run with.
DIODE2BASEPATH="../diode_2.0/"
SAMPLESBASEPATH="../samples/"

# Remove old config files if they exist
rm ./client_configs/default.conf
# Start the REST server
python3 $DIODE2BASEPATH/diode_rest.py --localhost --localdace &
SERVPID=$!
RETVAL=0

echo "server pid is: $SERVPID"


# Wait 10 seconds for the server to come online
sleep 10

echo $1
sh -c "$1 > cmdout.txt"
RETVAL=$?

if cat cmdout.txt | grep -q '+ exit 1'; then
    RETVAL=1
elif cat cmdout.txt | grep -q '+ exit 0'; then
    RETVAL=0
else
    echo "Failed to get sensible output"
    RETVAL=1
fi

if [ $RETVAL -eq 0 ]; then
    echo "TEST COMMAND SUCCESSFUL"
fi

# Terminate the server
kill $SERVPID

exit $RETVAL
