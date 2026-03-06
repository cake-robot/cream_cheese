import requests
import pandas as pd

def fetch_game_win_probabilities(game_id):
    # ESPN API endpoint for game summary
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary?event={game_id}"
    
    response = requests.get(url)
    
    if response.status_code == 200:
        game_data = response.json()

        # Extract the win probability data
        if 'winprobability' in game_data:
            win_probability = game_data['winprobability']
            
            # Create a list of dictionaries with relevant fields
            win_prob_list = [
                {
                    'Play ID': prob['playId'],
                    'Home Win Percentage': prob['homeWinPercentage'],
                    'Tie Percentage': prob['tiePercentage'],
                    'Seconds Left': prob['secondsLeft']
                }
                for prob in win_probability
            ]
            
            # Convert the list of dictionaries into a pandas DataFrame
            df = pd.DataFrame(win_prob_list)
            
            return df
        else:
            print("No win probability data found for this game.")
            return None
    else:
        print(f"Failed to fetch data: {response.status_code}")
        return None

# Example usage with a game_id
game_id = '401628483'  # Example game ID
df = fetch_game_win_probabilities(game_id)

if df is not None:
    print(df)
