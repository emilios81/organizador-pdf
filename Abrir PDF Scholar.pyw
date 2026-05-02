import runpy, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
runpy.run_path("main.py", run_name="__main__")
