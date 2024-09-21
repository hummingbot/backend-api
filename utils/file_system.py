import importlib
import inspect
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

import yaml
from hummingbot.client.config.config_data_types import BaseClientModel
from hummingbot.client.config.config_helpers import ClientConfigAdapter


class FileSystemUtil:
    """
    FileSystemUtil provides utility functions for file and directory management,
    as well as dynamic loading of script configurations.
    """
    base_path: str = "bots"  # Default base path

    def __init__(self, base_path: Optional[str] = None):
        """
        Initializes the FileSystemUtil with a base path.
        :param base_path: The base directory path for file operations.
        """
        if base_path:
            self.base_path = base_path

    def list_files(self, directory: str) -> List[str]:
        """
        Lists all files in a given directory.
        :param directory: The directory to list files from.
        :return: List of file names in the directory.
        """
        excluded_files = ["__init__.py", "__pycache__", ".DS_Store", ".dockerignore", ".gitignore"]
        dir_path = os.path.join(self.base_path, directory)
        return [f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f)) and f not in excluded_files]

    def list_folders(self, directory: str) -> List[str]:
        """
        Lists all folders in a given directory.
        :param directory: The directory to list folders from.
        :return: List of folder names in the directory.
        """
        dir_path = os.path.join(self.base_path, directory)
        return [d for d in os.listdir(dir_path) if os.path.isdir(os.path.join(dir_path, d))]

    def create_folder(self, directory: str, folder_name: str):
        """
        Creates a folder in a specified directory.
        :param directory: The directory to create the folder in.
        :param folder_name: The name of the folder to be created.
        """
        folder_path = os.path.join(self.base_path, directory, folder_name)
        os.makedirs(folder_path, exist_ok=True)

    def copy_folder(self, src: str, dest: str):
        """
        Copies a folder to a new location.
        :param src: The source folder to copy.
        :param dest: The destination folder to copy to.
        """
        src_path = os.path.join(self.base_path, src)
        dest_path = os.path.join(self.base_path, dest)
        os.makedirs(dest_path, exist_ok=True)
        for item in os.listdir(src_path):
            s = os.path.join(src_path, item)
            d = os.path.join(dest_path, item)
            if os.path.isdir(s):
                self.copy_folder(s, d)
            else:
                shutil.copy2(s, d)

    def copy_file(self, src: str, dest: str):
        """
        Copies a file to a new location.
        :param src: The source file to copy.
        :param dest: The destination file to copy to.
        """
        src_path = os.path.join(self.base_path, src)
        dest_path = os.path.join(self.base_path, dest)
        shutil.copy2(src_path, dest_path)

    def delete_folder(self, directory: str, folder_name: str):
        """
        Deletes a folder in a specified directory.
        :param directory: The directory to delete the folder from.
        :param folder_name: The name of the folder to be deleted.
        """
        folder_path = os.path.join(self.base_path, directory, folder_name)
        shutil.rmtree(folder_path)

    def delete_file(self, directory: str, file_name: str):
        """
        Deletes a file in a specified directory.
        :param directory: The directory to delete the file from.
        :param file_name: The name of the file to be deleted.
        """
        file_path = os.path.join(self.base_path, directory, file_name)
        os.remove(file_path)

    def path_exists(self, path: str) -> bool:
        """
        Checks if a path exists.
        :param path: The path to check.
        :return: True if the path exists, False otherwise.
        """
        return os.path.exists(os.path.join(self.base_path, path))

    def add_file(self, directory: str, file_name: str, content: str, override: bool = False):
        """
        Adds a file to a specified directory.
        :param directory: The directory to add the file to.
        :param file_name: The name of the file to be added.
        :param content: The content to be written to the file.
        :param override: If True, override the file if it exists.
        """
        file_path = os.path.join(self.base_path, directory, file_name)
        if not override and os.path.exists(file_path):
            raise FileExistsError(f"File '{file_name}' already exists in '{directory}'.")
        with open(file_path, 'w') as file:
            file.write(content)

    def append_to_file(self, directory: str, file_name: str, content: str):
        """
        Appends content to a specified file.
        :param directory: The directory containing the file.
        :param file_name: The name of the file to append to.
        :param content: The content to append to the file.
        """
        file_path = os.path.join(self.base_path, directory, file_name)
        with open(file_path, 'a') as file:
            file.write(content)

    @staticmethod
    def dump_dict_to_yaml(filename, data_dict):
        """
        Dumps a dictionary to a YAML file.
        :param data_dict: The dictionary to dump.
        :param filename: The file to dump the dictionary into.
        """
        with open(filename, 'w') as file:
            yaml.dump(data_dict, file)

    @staticmethod
    def read_yaml_file(file_path):
        """
        Reads a YAML file and returns the data as a dictionary.
        :param file_path: The path to the YAML file.
        :return: Dictionary containing the YAML file data.
        """
        with open(file_path, 'r') as file:
            data = yaml.safe_load(file)
        return data

    @staticmethod
    def load_script_config_class(script_name):
        """
        Dynamically loads a script's configuration class.
        :param script_name: The name of the script file (without the '.py' extension).
        :return: The configuration class from the script, or None if not found.
        """
        try:
            # Assuming scripts are in a package named 'scripts'
            module_name = f"bots.scripts.{script_name.replace('.py', '')}"
            if module_name not in sys.modules:
                script_module = importlib.import_module(module_name)
            else:
                script_module = importlib.reload(sys.modules[module_name])

            # Find the subclass of BaseClientModel in the module
            for _, cls in inspect.getmembers(script_module, inspect.isclass):
                if issubclass(cls, BaseClientModel) and cls is not BaseClientModel:
                    return cls
        except Exception as e:
            print(f"Error loading script class: {e}")  # Handle or log the error appropriately
        return None

    @staticmethod
    def ensure_file_and_dump_text(file_path, text):
        """
        Ensures that the directory for the file exists, then dumps the dictionary to a YAML file.
        :param file_path: The file path to dump the dictionary into.
        :param text: The text to dump.
        """
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w") as f:
            f.write(text)

    @staticmethod
    # TODO: make paths relative
    def get_connector_keys_path(account_name: str, connector_name: str) -> Path:
        return Path(f"bots/credentials/{account_name}/connectors/{connector_name}.yml")

    @staticmethod
    def save_model_to_yml(yml_path: Path, cm: ClientConfigAdapter):
        try:
            cm_yml_str = cm.generate_yml_output_str_with_comments()
            with open(yml_path, "w", encoding="utf-8") as outfile:
                outfile.write(cm_yml_str)
        except Exception as e:
            logging.error("Error writing configs: %s" % (str(e),), exc_info=True)

    def list_databases(self):
        archived_path = os.path.join(self.base_path, "archived")
        archived_instances = self.list_folders("archived")
        archived_databases = []
        for archived_instance in archived_instances:
            db_path = os.path.join(archived_path, archived_instance, "data")
            archived_databases += [os.path.join(db_path, db_file) for db_file in os.listdir(db_path)
                                   if db_file.endswith(".sqlite")]
        return archived_databases

    def list_checkpoints(self, full_path: bool):
        dir_path = os.path.join(self.base_path, "data")
        if full_path:
            checkpoints = [os.path.join(dir_path, f) for f in os.listdir(dir_path) if
                           os.path.isfile(os.path.join(dir_path, f))
                           and f.startswith("checkpoint") and f.endswith(".sqlite")]
        else:
            checkpoints = [f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f))
                           and f.startswith("checkpoint") and f.endswith(".sqlite")]
        return checkpoints
