from ollama import chat


stats = {
    "New York Knicks": {
        "Jalen Brunson": {
            "points": 45,
            "rebounds": 3,
            "assists": 3,
            "steals": 2,
            "blocks": 0,
            "turnovers": 3,
            "minutes": 41.1
        },
        "Josh Hart": {
            "points": 13,
            "rebounds": 11,
            "assists": 2,
            "steals": 0,
            "blocks": 0,
            "turnovers": 1,
            "minutes": 39.2
        },
        "Mikal Bridges": {
            "points": 14,
            "rebounds": 2,
            "assists": 4,
            "steals": 0,
            "blocks": 1,
            "turnovers": 0,
            "minutes": 38.6
        },
        "OG Anunoby": {
            "points": 11,
            "rebounds": 8,
            "assists": 0,
            "steals": 3,
            "blocks": 1,
            "turnovers": 1,
            "minutes": 32.8
        },
        "Karl-Anthony Towns": {
            "points": 2,
            "rebounds": 10,
            "assists": 1,
            "steals": 3,
            "blocks": 1,
            "turnovers": 5,
            "minutes": 22.7
        }
    },

    "San Antonio Spurs": {
        "De'Aaron Fox": {
            "points": 7,
            "rebounds": 0,
            "assists": 5,
            "steals": 2,
            "blocks": 0,
            "turnovers": 1,
            "minutes": 36.8
        },
        "Stephon Castle": {
            "points": 6,
            "rebounds": 5,
            "assists": 4,
            "steals": 1,
            "blocks": 0,
            "turnovers": 3,
            "minutes": 32.1
        },
        "Devin Vassell": {
            "points": 12,
            "rebounds": 7,
            "assists": 2,
            "steals": 2,
            "blocks": 1,
            "turnovers": 0,
            "minutes": 39.2
        },
        "Julian Champagnie": {
            "points": 14,
            "rebounds": 7,
            "assists": 1,
            "steals": 1,
            "blocks": 0,
            "turnovers": 4,
            "minutes": 30.7
        },
        "Victor Wembanyama": {
            "points": 19,
            "rebounds": 14,
            "assists": 2,
            "steals": 0,
            "blocks": 5,
            "turnovers": 2,
            "minutes": 37.9
        }
    }
}

while True:
    question = input("Ask a question (or type quit): ")

    if question.lower() == "quit":
        break

    prompt = f"""
Using only the statistics below, answer the question.
Data: {stats}

Question:
{question}
"""

    print("Entering model")

    response = chat(
        model='deepseek-r1:8b',
        messages=[{'role': 'user', 'content': prompt}]
    )

    print("\nAnswer:")
    print(response['message']['content'])