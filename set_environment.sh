#!/bin/bash

# Create or overwrite .env file
echo "Setting up .env file for the project...
By default, the current working directory will be used as the BOTS_PATH and the CONFIG_PASSWORD will be set to 'a'."

# Asking for CONFIG_PASSWORD and BOTS_PATH
CONFIG_PASSWORD=a
USERNAME=admin
PASSWORD=admin
BOTS_PATH=$(pwd)

# Write to .env file
echo "CONFIG_PASSWORD=$CONFIG_PASSWORD" > .env
echo "BOTS_PATH=$BOTS_PATH" >> .env
echo "USERNAME=$USERNAME" >> .env
echo "PASSWORD=$PASSWORD" >> .env
