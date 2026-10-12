# NHTSA fallback inbox

Normally empty. The daily sync downloads NHTSA's recall flat file by itself and
appends new car-seat recalls.

Only if the pipeline issue says NHTSA refused the download: download
`FLAT_RCL_POST_2010.zip` from NHTSA's datasets page
(https://www.nhtsa.gov/nhtsa-datasets-and-apis → Recalls → flat file) and upload it
here. The next run uses it once and deletes it.
