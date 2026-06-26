# Provenance Guard

## Architecture

### Submission Flow
                          +------------------+
                          |   Client/User    |
                          +--------+---------+
                                   |
                     POST /submit {text}
                                   |
                           [Rate Limiter]
                                   |
                                   v
                       +----------------------+
                       |  Submit Endpoint     |
                       +----------+-----------+
                                  |
                               raw text
                                  |
             +--------------------+--------------------+
             |                                         |
             v                                         v
+---------------------------+         +------------------------------+
| Signal 1                  |         | Signal 2                     |
| Groq Llama-3.3-70B        |         | Stylometric Heuristics       |
| AI probability            |         | burstiness, lexical diversity|
+-------------+-------------+         +--------------+---------------+
              | ai score                           | heuristic score
              +--------------------+---------------+
                                   |
                                   v
                     +----------------------------+
                     | Confidence Combiner        |
                     | weighted final confidence  |
                     +-------------+--------------+
                                   |
                 attribution + confidence + signals
                                   |
                                   v
                     +----------------------------+
                     | Transparency Label         |
                     +-------------+--------------+
                                   |
                           decision record
                                   |
                  +----------------+----------------+
                  |                                 |
                  v                                 v
           +---------------+                +---------------+
           | Audit Log     |                | API Response  |
           +---------------+                +---------------+


### Appeal Flow

Client
  |
POST /appeal {id, reason}
  |
  v
+-------------------+
| Appeal Endpoint   |
+---------+---------+
          |
 status = "under review"
          |
          +-------> Audit Log
          |
          v
     API Response