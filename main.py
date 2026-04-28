import os
import runpy
import sys

sys.argv[0] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'main.py')
runpy.run_path(sys.argv[0], run_name='__main__')
