import re
from pathlib import Path
from setuptools import setup, find_packages

with open("README.md", "r") as f:
    long_description = f.read()

txt = (Path(__file__).parent / 'aiohubot' / '__init__.py').read_text('utf-8')
version = re.findall(r"^__version__ = '([^']+)'\r?$", txt, re.M)[0]

setup(
    name="aiohubot",
    version=version,
    author="Lanfon",
    author_email="lanfon72@gmail.com",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/LFLab/aiohubot",
    packages=find_packages(),
    install_requires=["pyee>=6.*, <7", "aiohttp>=3.*, <4"],
    extras_require=dict(HTTP_AUTH=["aiohttp_basicauth"]),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7'
    ],
    python_requires='>=3.6',
)
