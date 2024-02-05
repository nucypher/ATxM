import sys

from twisted.logger import Logger, textFileLogObserver, globalLogPublisher

log = Logger("atxm")


observer = textFileLogObserver(sys.stdout)

globalLogPublisher.addObserver(observer)
