import os
from typing import List

import yaml


class FileSystemUtil:
    def __init__(self, base_path: str):
        self.base_path = base_path

    def list_files(self, directory: str) -> List[str]:
        dir_path = os.path.join(self.base_path, directory)
        return [f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f))]

    def list_folders(self, directory: str) -> List[str]:
        dir_path = os.path.join(self.base_path, directory)
        return [d for d in os.listdir(dir_path) if os.path.isdir(os.path.join(dir_path, d))]

    def add_file(self, directory: str, file_name: str, content: str, override: bool = False):
        file_path = os.path.join(self.base_path, directory, file_name)
        if not override and os.path.exists(file_path):
            raise FileExistsError(f"File '{file_name}' already exists in '{directory}'.")
        with open(file_path, 'w') as file:
            file.write(content)

    @staticmethod
    def dump_dict_to_yaml(data_dict, filename):
        with open(filename, 'w') as file:
            yaml.dump(data_dict, file)

    @staticmethod
    def read_yaml_file(file_path):
        with open(file_path, 'r') as file:
            data = yaml.safe_load(file)
        return data
