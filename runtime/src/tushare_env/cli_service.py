"""Detached dashboard entry point."""
import sys
from qt_research.dashboard import main
if __name__=='__main__':main(['serve',*sys.argv[1:]])
