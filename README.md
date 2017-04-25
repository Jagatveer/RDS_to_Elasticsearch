# RDS_to_Elasticsearch
Stream RDS postgresql logs directly and continuosly to elasticsearch

This is a modified version of my work at [rds_tail!](https://github.com/Jagatveersingh/rds_tail), which streams the logs to your local system.

I have updated the script so that the entire log goes as a json in elasticsearch for better search ability. simple json.dump did the thing but the message field had the log data in string format. So feel free to use it that way if you like. There is some cleanup to do in the script, I will do it in some free time. :)
