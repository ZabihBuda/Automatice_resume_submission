// Mock of the newer Greenhouse application form (job-boards.greenhouse.io), whose
// dropdowns are react-select comboboxes rather than <select> elements.
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import Select from "react-select";

const yn = ["Yes", "No"].map(v => ({ value: v, label: v }));
const opts = a => a.map(v => ({ value: v, label: v }));
const QUESTIONS = [
  { id: "question_1", label: "Are you currently authorized to work in the U.S.?", options: yn },
  { id: "question_2", label: "Do you now, or will you in the future, require immigration sponsorship to work at Acme?", options: yn },
  { id: "question_3", label: 'By selecting "I agree," I understand that the information I have provided will be processed in accordance with Acme\'s Candidate Privacy Policy.', options: opts(["I agree"]) },
  { id: "question_4", label: "Country", options: opts(["Afghanistan", "Brazil", "Canada", "Mexico", "United Kingdom", "United States"]) },
  { id: "question_5", label: "Have you ever worked for Acme before, as an employee or a contractor/consultant?", options: yn },
  { id: "question_6", label: "What gender identity do you most closely identify with?", options: opts(["Man", "Woman", "Non-binary", "I don't wish to answer"]) },
  { id: "question_7", label: "Are you a person of transgender experience?", options: opts(["Yes", "No", "I don't wish to answer"]) },
  { id: "question_8", label: "Are you a veteran/have you served in the military?", options: opts(["I am a veteran", "I am not a veteran", "I don't wish to answer"]) },
  { id: "question_9", label: "Please select up to 2 ethnicities that you most closely identify with.", multi: true,
    options: opts(["Asian", "Black or African American", "Hispanic or Latinx", "White", "I don't wish to answer"]) },
  { id: "question_10", label: "Do you live with a disability (as outlined by the ADA)?", options: opts(["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"]) },
];

function App() {
  const [vals, setVals] = useState({});
  const [errors, setErrors] = useState([]);
  const submit = e => {
    e.preventDefault();
    const missing = QUESTIONS.filter(q => !vals[q.id] || (q.multi && !vals[q.id].length));
    const text = Array.from(document.querySelectorAll("input[type=text]")).filter(i => i.required && !i.value);
    if (missing.length || text.length) { setErrors([...missing.map(q => q.id), ...text.map(i => i.id)]); return; }
    window.__submitted = vals;
    document.body.innerHTML = "<h1>Thank you for applying.</h1><p>Your application has been received.</p>";
  };
  return (
    <form onSubmit={submit} noValidate>
      <div className="field"><label htmlFor="first_name">First Name<span>*</span></label><input type="text" id="first_name" required /></div>
      <div className="field"><label htmlFor="last_name">Last Name<span>*</span></label><input type="text" id="last_name" required /></div>
      <div className="field"><label htmlFor="email">Email<span>*</span></label><input type="text" id="email" required /></div>
      <div className="field"><label htmlFor="resume">Resume/CV<span>*</span></label><input type="file" id="resume" /></div>
      <div className="field"><label htmlFor="question_0">Please provide the name of your current (or most recent) company<span>*</span></label>
        <input type="text" id="question_0" required aria-required="true" /></div>
      {QUESTIONS.map(q => (
        <div className="select" key={q.id}>
          <div className="select__container">
            <label id={q.id + "-label"} htmlFor={q.id} className="label select__label">{q.label}<span aria-hidden="true">*</span></label>
            <Select inputId={q.id} instanceId={q.id} classNamePrefix="select" isMulti={!!q.multi} options={q.options}
              aria-labelledby={q.id + "-label"} aria-required="true" placeholder="Select..."
              onChange={v => setVals({ ...vals, [q.id]: v })} />
            {errors.includes(q.id) && <p className="helper-text--error">This field is required.</p>}
          </div>
        </div>
      ))}
      <button type="submit" className="btn btn--pill">Submit application</button>
    </form>
  );
}
createRoot(document.getElementById("root")).render(<App />);
