import os
import importlib
import sys
import inspect
from typing import List
import yaml
from hummingbot.client.config.config_data_types import BaseClientModel


class FileSystemUtil:
    """
    FileSystemUtil provides utility functions for file and directory management,
    as well as dynamic loading of script configurations.
    """

    def __init__(self, base_path: str):
        """
        Initializes the FileSystemUtil with a base path.
        :param base_path: The base directory path for file operations.
        """
        self.base_path = base_path

    def list_files(self, directory: str) -> List[str]:
        """
        Lists all files in a given directory.
        :param directory: The directory to list files from.
        :return: List of file names in the directory.
        """
        dir_path = os.path.join(self.base_path, directory)
        return [f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f))]

    def list_folders(self, directory: str) -> List[str]:
        """
        Lists all folders in a given directory.
        :param directory: The directory to list folders from.
        :return: List of folder names in the directory.
        """
        dir_path = os.path.join(self.base_path, directory)
        return [d for d in os.listdir(dir_path) if os.path.isdir(os.path.join(dir_path, d))]

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

    @staticmethod
    def dump_dict_to_yaml(data_dict, filename):
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
