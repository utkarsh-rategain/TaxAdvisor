import os
from flask import Flask, render_template, request, redirect, url_for, flash
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
import uuid
from PIL import Image
import pytesseract
from pdf2image import convert_from_path
import PyPDF2
import requests
import json
from tax_calculator import calculate_tax_old, calculate_tax_new
import psycopg2
from datetime import datetime
from decimal import Decimal
import markdown2

load_dotenv()

app = Flask(__name__, template_folder='templates')

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

CONVO_LOG = 'ai_conversation_log.json'

# Helper to check allowed file
def allowed_file(filename):
    allowed_ext = {'pdf', 'png', 'jpg', 'jpeg'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_ext

def extract_text_from_file(filepath):
    ext = filepath.rsplit('.', 1)[1].lower()
    if ext == 'pdf':
        # Try text extraction from PDF
        try:
            with open(filepath, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                text = " ".join(page.extract_text() or '' for page in reader.pages)
            if text.strip():
                return text
        except Exception:
            pass
        # If text extraction fails, use OCR
        images = convert_from_path(filepath)
        text = ''
        for img in images:
            text += pytesseract.image_to_string(img)
        return text
    elif ext in {'png', 'jpg', 'jpeg'}:
        img = Image.open(filepath)
        text = pytesseract.image_to_string(img)
        return text
    return ''

def call_gemini_api(prompt):
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        print('[ERROR] GEMINI_API_KEY is missing from environment!')
        return None
    url = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key=' + api_key
    headers = {'Content-Type': 'application/json'}
    data = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    response = requests.post(url, headers=headers, data=json.dumps(data))
    if response.status_code == 200:
        try:
            return response.json()['candidates'][0]['content']['parts'][0]['text']
        except Exception as e:
            print('[ERROR] Could not parse Gemini response:', e)
            return None
    else:
        print(f'[ERROR] Gemini API call failed. Status: {response.status_code}, Response: {response.text}')
        return None

def build_gemini_prompt(raw_text):
    return (
        "Extract the following fields from the salary slip text. "
        "If the slip is for a single month, annualize all monthly values (multiply by 12). "
        "Return a JSON object with these keys: gross_salary, basic_salary, hra_received, rent_paid, "
        "deduction_80c, deduction_80d, standard_deduction, professional_tax, tds. "
        "If a value is missing, set it to 0.\n\n"
        f"Salary Slip Text:\n{raw_text}\n\nJSON:"
    )

def clean_gemini_json_response(response_text):
    if not response_text:
        return ''
    # Remove triple backticks and language tags
    cleaned = response_text.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.lstrip('`')
        # Remove language tag if present
        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    if cleaned.endswith('```'):
        cleaned = cleaned.rstrip('`').strip()
    return cleaned

# Helper to load/save conversation log

def load_conversation(session_id):
    try:
        with open(CONVO_LOG, 'r') as f:
            logs = json.load(f)
        return logs.get(session_id, [])
    except Exception:
        return []

def save_conversation(session_id, convo):
    try:
        try:
            with open(CONVO_LOG, 'r') as f:
                logs = json.load(f)
        except Exception:
            logs = {}
        logs[session_id] = convo
        with open(CONVO_LOG, 'w') as f:
            json.dump(logs, f, indent=2)
    except Exception as e:
        print('[ERROR] Could not save conversation log:', e)

def convert_decimals(obj):
    if isinstance(obj, dict):
        return {k: convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_decimals(i) for i in obj]
    elif isinstance(obj, Decimal):
        return float(obj)
    else:
        return obj

# Gemini advisor prompt builder

def build_advisor_prompt(user_data, tax_old, tax_new, selected_regime, convo):
    history = ''
    for turn in convo:
        if turn['role'] == 'user':
            history += f"User: {turn['content']}\n"
        else:
            history += f"Advisor: {turn['content']}\n"
    user_data_clean = convert_decimals(user_data)
    prompt = f"""
You are a thoughtful, step-by-step tax advisor for Indian salaried individuals. Your goal is to help the user optimize their taxes and investments. Think step by step, ask clarifying questions if needed, and always step back to consider the bigger goal of maximizing tax savings and financial well-being.

Here is the user's financial data and tax results:
{json.dumps(user_data_clean, indent=2)}
Old Regime Tax: ₹{tax_old}
New Regime Tax: ₹{tax_new}
User Selected Regime: {selected_regime}

Conversation so far:
{history}

If you need more information, ask a smart, contextual follow-up question. If you have enough information, provide a clear, actionable, personalized tax-saving and investment advice. Always explain your reasoning. Only ask one question at a time. If you are giving advice, do not ask further questions.

**When giving advice, format your response using Markdown for headings, bold, and lists.**
"""
    return prompt

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash('No selected file')
            return redirect(request.url)
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            session_id = str(uuid.uuid4())
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], session_id + '_' + filename)
            file.save(save_path)
            return redirect(url_for('extract', session_id=session_id, filename=filename))
    return render_template('upload.html')

@app.route('/extract')
def extract():
    session_id = request.args.get('session_id')
    filename = request.args.get('filename')
    if not session_id or not filename:
        flash('Invalid session or file.')
        return redirect(url_for('upload'))
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], session_id + '_' + filename)
    raw_text = extract_text_from_file(filepath)
    print('\n[DEBUG] Extracted Text:\n', raw_text)
    prompt = build_gemini_prompt(raw_text)
    print('\n[DEBUG] Gemini Prompt:\n', prompt)
    gemini_response = call_gemini_api(prompt)
    print('\n[DEBUG] Gemini Response:\n', gemini_response)
    try:
        cleaned_response = clean_gemini_json_response(gemini_response)
        extracted_data = json.loads(cleaned_response)
    except Exception as e:
        print('[ERROR] Could not parse cleaned Gemini response:', e)
        extracted_data = {
            'gross_salary': 0,
            'basic_salary': 0,
            'hra_received': 0,
            'rent_paid': 0,
            'deduction_80c': 0,
            'deduction_80d': 0,
            'standard_deduction': 0,
            'professional_tax': 0,
            'tds': 0
        }
    return render_template('form.html', data=extracted_data, session_id=session_id, filename=filename)

@app.route('/calculate', methods=['POST'])
def calculate():
    data = {
        'session_id': request.form.get('session_id'),
        'gross_salary': request.form.get('gross_salary', 0),
        'basic_salary': request.form.get('basic_salary', 0),
        'hra_received': request.form.get('hra_received', 0),
        'rent_paid': request.form.get('rent_paid', 0),
        'deduction_80c': request.form.get('deduction_80c', 0),
        'deduction_80d': request.form.get('deduction_80d', 0),
        'standard_deduction': request.form.get('standard_deduction', 0),
        'professional_tax': request.form.get('professional_tax', 0),
        'tds': request.form.get('tds', 0)
    }
    selected_regime = request.form.get('regime', 'new')
    session_id = data['session_id']
    # Calculate taxes
    tax_old = calculate_tax_old(data)
    tax_new = calculate_tax_new(data)
    best_regime = 'old' if tax_old < tax_new else 'new'
    # Save to Supabase
    db_url = os.getenv('DB_URL')
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        # Insert or update UserFinancials
        cur.execute('''INSERT INTO UserFinancials (session_id, gross_salary, basic_salary, hra_received, rent_paid, deduction_80c, deduction_80d, standard_deduction, professional_tax, tds) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (session_id) DO UPDATE SET gross_salary=EXCLUDED.gross_salary, basic_salary=EXCLUDED.basic_salary, hra_received=EXCLUDED.hra_received, rent_paid=EXCLUDED.rent_paid, deduction_80c=EXCLUDED.deduction_80c, deduction_80d=EXCLUDED.deduction_80d, standard_deduction=EXCLUDED.standard_deduction, professional_tax=EXCLUDED.professional_tax, tds=EXCLUDED.tds''',
            (session_id, data['gross_salary'], data['basic_salary'], data['hra_received'], data['rent_paid'], data['deduction_80c'], data['deduction_80d'], data['standard_deduction'], data['professional_tax'], data['tds']))
        # Insert or update TaxComparison
        cur.execute('''INSERT INTO TaxComparison (session_id, tax_old_regime, tax_new_regime, best_regime, selected_regime) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (session_id) DO UPDATE SET tax_old_regime=EXCLUDED.tax_old_regime, tax_new_regime=EXCLUDED.tax_new_regime, best_regime=EXCLUDED.best_regime, selected_regime=EXCLUDED.selected_regime''',
            (session_id, tax_old, tax_new, best_regime, selected_regime))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print('[ERROR] Could not save to Supabase:', e)
    return render_template('results.html', tax_old=tax_old, tax_new=tax_new, best_regime=best_regime, selected_regime=selected_regime, data=data)

@app.route('/advisor', methods=['GET', 'POST'])
def advisor():
    session_id = request.args.get('session_id') or request.form.get('session_id')
    if not session_id:
        flash('Session missing.')
        return redirect(url_for('index'))
    # Load user data and tax results from DB
    db_url = os.getenv('DB_URL')
    user_data = {}
    tax_old = tax_new = 0
    selected_regime = 'new'
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute('SELECT gross_salary, basic_salary, hra_received, rent_paid, deduction_80c, deduction_80d, standard_deduction, professional_tax, tds FROM UserFinancials WHERE session_id=%s', (session_id,))
        row = cur.fetchone()
        if row:
            user_data = {
                'gross_salary': row[0],
                'basic_salary': row[1],
                'hra_received': row[2],
                'rent_paid': row[3],
                'deduction_80c': row[4],
                'deduction_80d': row[5],
                'standard_deduction': row[6],
                'professional_tax': row[7],
                'tds': row[8]
            }
        cur.execute('SELECT tax_old_regime, tax_new_regime, selected_regime FROM TaxComparison WHERE session_id=%s', (session_id,))
        row = cur.fetchone()
        if row:
            tax_old, tax_new, selected_regime = row
        cur.close()
        conn.close()
    except Exception as e:
        print('[ERROR] Could not load user/tax data for advisor:', e)
    # Load conversation history
    convo = load_conversation(session_id)
    if request.method == 'POST':
        user_msg = request.form.get('user_message', '').strip()
        if user_msg:
            convo.append({'role': 'user', 'content': user_msg, 'timestamp': datetime.now().isoformat()})
    # Build prompt and get Gemini's response
    prompt = build_advisor_prompt(user_data, tax_old, tax_new, selected_regime, convo)
    gemini_response = call_gemini_api(prompt)
    print('\n[DEBUG] Advisor Gemini Response:\n', gemini_response)
    # Clean and add Gemini's response to convo
    if gemini_response:
        cleaned = clean_gemini_json_response(gemini_response) if gemini_response.strip().startswith('```') else gemini_response.strip()
        convo.append({'role': 'advisor', 'content': cleaned, 'timestamp': datetime.now().isoformat()})
    save_conversation(session_id, convo)
    # Find last advisor message
    last_advisor = next((turn['content'] for turn in reversed(convo) if turn['role'] == 'advisor'), '')
    advisor_message_html = markdown2.markdown(last_advisor) if last_advisor else ''
    return render_template('ask.html', advisor_message_html=advisor_message_html, session_id=session_id)

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)