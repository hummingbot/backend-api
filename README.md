# Backend API 

## Overview
Backend-api is a dedicated solution for managing Hummingbot instances. It offers a robust backend API to streamline the deployment, management, and interaction with Hummingbot containers. This tool is essential for administrators and developers looking to efficiently handle various aspects of Hummingbot operations.

## Features
- **Deployment File Management**: Manage files necessary for deploying new Hummingbot instances.
- **Container Control**: Effortlessly start and stop Hummingbot containers.
- **Archiving Options**: Securely archive containers either locally or on Amazon S3 post-removal.
- **Direct Messaging**: Communicate with Hummingbots through the broker for effective control and coordination.

## Getting Started
### Installation
1. Install the environment using Conda:
   ```bash
   conda env create -f environment.yml
   ```
2. Activate the Conda environment:
   ```bash
   conda activate [your-env-name]
   ```

### Running the API
Run the API using uvicorn with the following command:
   ```bash
   uvicorn main:app --reload
   ```

## Usage
This api is designed to:
- **Deploying Hummingbot instances**
- **Starting/Stopping Containers**
- **Archiving Hummingbots**
- **Messaging with Hummingbot instances**

To test this endpoints you can use the [Swagger UI](http://localhost:8000/docs) or [Redoc](http://localhost:8000/redoc).


## Contributing
Contributions are welcome! For support or queries, please contact us on Discord.
