from setuptools import setup, find_packages

setup(
    name="TerraLab",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "PyQt5",
        "numpy",
        "skyfield"
    ],
    entry_points={
        'console_scripts': [
            'terralab=TerraLab.__main__:main',
        ],
    },
)
