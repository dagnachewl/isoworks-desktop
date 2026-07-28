from setuptools import setup, find_packages

setup(
    name="isoworks_core",
    version="1.0.0",
    description="IsoWorks Core Data Processing and Parsing Engines",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[
        "sqlalchemy>=2.0",
        "numpy",
        "pandas",
        "scipy",
        "scikit-learn",
        "openpyxl"
    ]
)
