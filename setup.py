from setuptools import find_packages, setup

setup(
    name="terralab",
    version="0.1.0",
    packages=find_packages(include=["TerraLab", "TerraLab.*"]),
    install_requires=[
        "numpy>=1.26",
    ],
)
