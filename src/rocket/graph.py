from typing import TypedDict
from typing import List

from langchain_core.messages import SystemMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import MessagesState
from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from typing import Dict, Any
from langchain_mistralai.embeddings import MistralAIEmbeddings
from langchain_community.vectorstores import FAISS
import os
from dotenv import load_dotenv
load_dotenv()



import os

embeddings = MistralAIEmbeddings(model="mistral-embed")
index_path = os.path.join(os.path.dirname(__file__), "faiss_index")
vector = FAISS.load_local(index_path, embeddings, allow_dangerous_deserialization=True)



retriever = vector.as_retriever()



class GraphState(MessagesState):
    question: str
    documents: List[str]
    retry_search: bool
    is_relevant: bool




        # -- 2. Grader model: yes/no for question relevance --
class RelevanceGrade(BaseModel):
  binary_score: str = Field(description="Is the user's question related to DoDFMR Volume 7A? Answer 'yes' or 'no'.")


        # -- 3. LLM setup --
llm = ChatOpenAI(model_name="gpt-4o", temperature=0)
structured_grader = llm.with_structured_output(RelevanceGrade)


        # -- 4. Prompt for routing decision --
relevance_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are a classifier. Determine whether a user's question is related to the DoD Financial Management Regulation Volume 7A (military pay and entitlements).
If the question is about military pay, entitlements, leave, bonuses, allowances, or anything related to finance per DoDFMR 7A, answer "yes".
If it’s a greeting, thank you, small-talk, or unrelated, answer "no".""",
        ),
        ("human", "User question: {question}"),
    ]
)

        # -- 5. Combine prompt and structured output LLM --
relevance_chain = relevance_prompt | structured_grader


     # -- 6. LangGraph node: sets is_relavent = True or False --
def relevance(state: GraphState):
    print("--Checking if question is related to DoDFMR 7A--")
    question = state["messages"][-1]

    score = relevance_chain.invoke({"question": question})


    grade = score.binary_score

    if grade.lower() == "yes":
        print("---GRADE: DOCUMENT RELEVANT---")
        is_relevant = True
    else:
      print("---GRADE: DOCUMENT NOT RELEVANT---")
      is_relevant = False



    return {"is_relevant": is_relevant}



from typing import Any, Dict


#Retrieve Node


def retrieve(state:GraphState):
  print("--Retriviening that hoe--")

  question= state["messages"][-1].content
  print(question)
  documents= retriever.invoke(str(question))
  print(documents)

  return {"documents": documents}




#Document Grader Node
from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

llm = ChatOpenAI(model_name="gpt-4o", temperature=0)

class GradeDocuments(BaseModel):
  binary_score: str = Field(description= "documents are relavant to the question yes or no")

structured_llm_grader = llm.with_structured_output(GradeDocuments)

system = """You are a grader assessing relevance of a retrieved document to a user question. \n Add commentMore actions
    If the document contains keyword(s) or semantic meaning related to the question, grade it as relevant. \n
    Give a binary score 'yes' or 'no' score to indicate whether the document is relevant to the question."""
grade_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", system),
        ("human", "Retrieved document: \n\n {document} \n\n User question: {question}"),
    ]
)

retrieval_grader = grade_prompt | structured_llm_grader

def grade_documents(state:GraphState) -> Dict[str, Any]:
  print("--Grading documents--")
  documents=state["documents"]
  question= state["messages"][-1].content

  filtered_docs = []
  retry_index = False
  for d in documents:
        score = retrieval_grader.invoke(
            {"question": question, "document": d.page_content}
        )
        grade = score.binary_score
        if grade.lower() == "yes":
            print("---GRADE: DOCUMENT RELEVANT---")
            filtered_docs.append(d)
        else:
            print("---GRADE: DOCUMENT NOT RELEVANT---")
            retry_index = True
            continue
  return {"documents": filtered_docs, "retry_index": retry_index}


from langchain import hub
from langchain_core.output_parsers import StrOutputParser
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model= "gpt-4o",temperature=0)




from typing import Any, Dict


def generate(state: GraphState) -> Dict[str, Any]:
    print("---GENERATE---")
    question = state["messages"][-1].content
    print(f"{question}")
    documents = state.get("documents", [])
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    rag_prompt = """
    You are an assistant for question-answering tasks. 
            Use the following pieces of retrieved context to answer the question.if there is no context be nice and friendly, If you don't know the answer,
            just say that it is not in your scope of information.
            Use three sentences maximum and keep the answer concise.
            Question:{question} Context: {context}
    """

    rag_instructions = rag_prompt.format(question=question, context=documents)

    generation = llm.invoke([SystemMessage(content=rag_instructions)])
    print(generation)
    return {"messages": state["messages"] + [generation]}





from langgraph.graph import END, StateGraph


def decide_to_generate(state):
    print("---ASSESS QUESTION RELEVANCY---")


    if state["is_relevant"]:
        print(
            "---DECISION: RETRIEVE---"
        )
        return "retrieve"
    else:
        print("---DECISION: GENERATE---")
        return "generate"


builder = StateGraph(GraphState)

builder.add_node("check_relevance", relevance)
builder.add_node("retrieve", retrieve)
builder.add_node("generate", generate)
builder.add_node("grade_documents", grade_documents)

builder.set_entry_point("check_relevance")
builder.add_conditional_edges("check_relevance", decide_to_generate, {
"retrieve":"retrieve",
"generate":"generate"

})

builder.add_edge("retrieve", "grade_documents")
builder.add_edge("grade_documents", "generate")
builder.add_edge("generate", END)

graph= builder.compile()


# graph.get_graph().draw_mermaid_png(output_file_path="graph.png")

