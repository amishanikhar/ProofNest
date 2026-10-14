# ProofNest

## Run locally (no AWS needed)
    pip install -r requirements.txt
    python app.py          # open http://localhost:5000
Live camera works on localhost. Data goes to ./proofnest.db (SQLite) and ./data/files.

## Run on AWS
Environment variables (set before starting gunicorn):
    SECRET_KEY=<long random string>
    DATABASE_URL=mysql+pymysql://USER:PASS@<rds-endpoint>:3306/proofnest   (create DB `proofnest` first)
    S3_BUCKET=<private bucket with Versioning enabled>
    DATA_DIR=/mnt/data          (the extra EBS volume - Practical 1/3)
Start:  gunicorn -w 2 -b 0.0.0.0:80 app:app   (or behind the Load Balancer)

EC2 IAM role needs: s3:PutObject, GetObject, GetObjectVersion, ListBucket, ListBucketVersions
on the bucket. No access keys in code.
Camera: browsers allow live camera only on HTTPS or localhost. Over plain HTTP the
"Choose file" button still opens the phone camera. Add HTTPS via ALB + ACM certificate.
